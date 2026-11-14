"""Replay the state graph of the flush model (``tests/models/flush.tla``) against the real claims, submissions and settle.

TLC wrote the graph of ``flush_traces.cfg``, one slot of two periods and two ideas (``-dump dot,actionlabels flush_traces.dot``), and
``scripts/graph_traces.py`` turned it into traces that take every kind of transition that the driver checks, once at
least (``--by idea,held,submitted,best``; ``tests/traces/flush``). Each trace has a campaign of its own, in a git repo
whose runs each work in a worktree. Each step runs the code that the model's action cites: a queue a run at the type's
base, a claim ``backlog.ops.claim``, a submit ``submissions.submit`` for that claim, a timeout and a capture
``run_outcome`` then ``RunController._close``, a settle ``RunController._settle``, which settles the run's claims once
its submissions are scored. A restart is the daemon's stop or crash, then the next start: the planner's ``fail_open``
fails the run, the one open run of the trace's one slot, and a settle after it the next start's ``resume`` of it
(``resume_unscored``). The scores are the boundary: a score, a
confirmation and a new best are measurements written as the model says, recorded by the real ``record_scored``, and
the pool says which candidate is the best. After each step every idea, every run's claims, end and base, the best and
the places under ``max_runs`` are the model's.
"""

import re
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from git import Repo
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.db.models import AgentSession, Campaign
from piloop.db.session import Database
from piloop.ids import Revision, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.contract import TypeHost
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import submissions
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.pool import Pool, best_of, track_of
from piloop.plugins.campaign_type.kit.pool.scored import record_scored
from piloop.plugins.campaign_type.kit.runs import stop as runs_stop
from piloop.plugins.campaign_type.kit.runs.controller import RunController, RunFacts, run_outcome
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.optimization import Optimization
from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner
from piloop.plugins.vcs.git import Git
from piloop.plugins.vcs.git.commands import Identity, commit_env
from tests.conftest import HARNESS_APPEND, ScriptRuntime, register, write_toml
from tests.daemon.test_pool import controller
from tests.daemon.test_submissions import at_once
from tests.test_lease_model import State, Value, _at, _steps

TRACES = Path(__file__).parent.parent / 'traces' / 'flush'
WORKER = Identity(name='worker', email='worker@localhost')
ENDS = {
    'none': None,
    'finished': RunEnd.FINISHED,
    'timed_out': RunEnd.TIMED_OUT,
    'stopped': RunEnd.CAMPAIGN_STOPPED,
    'restarted': RunEnd.DAEMON_STOPPED,
}
"""Each end of the model, as ``runs.end``."""
NOISE = 2
"""The evaluations that confirm a candidate: a better first score waits for its confirmation."""
SUBS = {'none': 'none', 'waiting': 'waiting', 'better': 'waiting', 'confirmed': 'waiting', 'not': 'not', 'best': 'best'}
"""What the code shows of each state of a submission: a better or confirmed one waits for its score too."""


@dataclass
class Replay:
    """One trace's campaign, its runs and their worktrees, the ideas, and the candidates that were the best in turn."""

    db: Database
    controller: RunController
    planner: OptimizationPlanner
    campaign: Campaign
    path: Path
    ideas: dict[int, IdeaId]
    bests: list[CandidateId]
    """The best after each new best: the baseline first."""
    restarted: set[int] = field(default_factory=set)
    """The runs that a restart failed: the next start resumes their scoring."""
    runs: dict[int, RunId] = field(default_factory=dict)
    worktrees: dict[int, Path] = field(default_factory=dict)
    agents: dict[int, Agent] = field(default_factory=dict)
    based: dict[int, CandidateId] = field(default_factory=dict)
    """Each run's base at its first submission, which no later claim moves."""

    def step(self, state: State) -> None:
        r, i = _pair(state['arg'])
        match state['op']:
            case 'queue':
                self._queue(r)
            case 'claim':
                with self.db.session() as s:
                    backlog_ops.claim(s, self.ideas[i], actor=self.agents[r])
                    s.commit()
            case 'submit':
                self._submit(r, i)
            case 'score':
                self._score(i, better=_at(state, 'sub', i) == 'better')
            case 'confirm':
                if _at(state, 'sub', i) == 'not':  # its second evaluation pulls its mean below the best's
                    self._measure(i, -1000.0, record=True)
            case 'best':
                self._new_best(i)
            case 'wrapup' | 'timeout':
                pass  # the note refuses nothing, and the runtime's kill changes no row: the capture reads its outcome
            case 'capture':
                self._capture(r, stop_cut=bool(_at(state, 'cut', r)))
            case 'settle':
                self._settle(r)
            case 'restart':
                self._restart(r)
            case 'stop':
                pass  # it cuts each working implementer: the capture reads that the stop cut it
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        self.check(state)

    def _queue(self, r: int) -> None:
        """A run at the type's base, with its implementer, as ``OptimizationPlanner.queue`` starts one."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            base = Optimization().base(s, campaign)
            assert base is not None
            run = Run(campaign_id=campaign.id, base_candidate_id=base.id, harness='script', model='m')
            run.status, run.started_at = RunStatus.RUNNING, campaign.created_at
            s.add(run)
            s.flush()
            agent = AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, subject=TargetRef(RUN, run.id))
            s.add(agent)
            s.commit()
            self.runs[r], self.agents[r] = run.id, Agent(role=IMPLEMENTER, session=agent.id)
            revision = base.revision
        self.worktrees[r] = self.path.parent / f'{self.path.name}-r{r}'
        Repo(self.path).git.worktree('add', '-q', '--detach', str(self.worktrees[r]), revision)

    def _submit(self, r: int, i: int) -> None:
        """A commit of run ``r``'s worktree for its claim of idea ``i``: a file of its own, so a tree of its own."""
        work = self.worktrees[r]
        (work / f'idea{i}.txt').write_text(f'run {r}\n')
        repo = Repo(work)
        repo.git.add('-A')
        repo.git.commit('-qm', f'idea {i} in run {r}', env=commit_env(WORKER, WORKER))
        revision = Revision(repo.head.commit.hexsha)
        with self.db.session() as s, closing(Git().open(self.path)) as vcs:
            run = s.get_one(Run, self.runs[r])
            self.based.setdefault(r, run.base_candidate_id)
            made = submissions.submit(s, run, vcs.version(revision), vcs, self.agents[r], self.ideas[i])
            assert made.idea_id == self.ideas[i], 'a submission names its claim'
            s.commit()

    def _submission(self, i: int) -> CandidateId:
        """The submission of the newest claim of idea ``i``."""
        with self.db.session() as s:
            [found] = submissions.submitted_for(s, s.get_one(Idea, self.ideas[i]).claims[-1])
            return found

    def _measure(self, i: int, value: float, *, times: int = 1, record: bool) -> None:
        """Measure idea ``i``'s submission at ``value`` ``times``, then record its score if ``record``."""
        candidate = self._submission(i)
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            assert campaign.evaluator_id is not None
            for _ in range(times):
                metrics = {'score': value, 'size': 1.0}
                s.add(Measurement(candidate_id=candidate, evaluator_id=campaign.evaluator_id, metrics=metrics))
            s.flush()
            if record:
                found = s.get_one(Candidate, candidate)
                record_scored(s, Pool.of(s, campaign, track_of(campaign.config, found)), candidate)
            s.commit()

    def _top(self) -> float:
        """The mean of the best's primary."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
            mean = pool.mean(pool.best())
            assert mean is not None
            return mean

    def _score(self, i: int, *, better: bool) -> None:
        """The first evaluation: above the best for a better score, which waits for its confirmation, else below."""
        if better:
            self._measure(i, self._top() + 10, record=False)
        else:
            self._measure(i, 0.0, record=True)

    def _new_best(self, i: int) -> None:
        """Confirm the submission above the best, and record it: it is the best now."""
        self._measure(i, self._top() + 10, times=NOISE, record=True)
        self.bests.append(self._submission(i))

    def _capture(self, r: int, *, stop_cut: bool) -> None:
        """Close the run as ``run_outcome`` says its timeout ends it, as ``RunController._ran`` does (``_close``).

        ``_ran`` then settles it (``_settle``). The model's settle is that, once the scores came; a run with no claim
        has no settle in the model, and nothing to wait for in ``_settle``, which ends its scoring now.
        """
        with self.db.session() as s:
            run = s.get_one(Run, self.runs[r])
            sent = bool(submissions.submitted(s, run.id))
            claimed = bool(run.claims)
        end, error = run_outcome(RunFacts(timed_out=True, stop_cut=stop_cut, error='agent timeout', submitted=sent))
        with Tracer(None).span('piloop.run') as span:
            self.controller._close(span, self.runs[r], end, error)
        if not claimed:
            self.controller._settle(self.runs[r], end)

    def _settle(self, r: int) -> None:
        """Settle run ``r``'s claims as the code does once each of its submissions has its score: ``_settle`` after its
        capture, and after a restart the next start's ``resume``."""
        with self.db.session() as s:
            assert submissions.all_scored(s, self.runs[r]), 'its claims settle once its submissions are scored'
            end = s.get_one(Run, self.runs[r]).end
        assert end is not None
        if r in self.restarted:
            self.controller.resume(self.runs[r])
        else:
            self.controller._settle(self.runs[r], end)

    def _restart(self, r: int) -> None:
        """The daemon stops, or crashes, and starts again: ``fail_open`` fails each open run, here run ``r`` alone."""
        with self.db.session() as s:
            open_runs = s.scalars(select(Run.id).where(Run.campaign_id == self.campaign.id, Run.end.is_(None))).all()
            assert list(open_runs) == [self.runs[r]], 'one slot: the restart fails its run only'
            self.planner.fail_open(s, RunEnd.DAEMON_STOPPED, reaped=False, sparing=())
            s.commit()
        self.restarted.add(r)

    def check(self, state: State) -> None:
        """The code's state is the model's: each idea and its submission, each run's claims, end and base, the best."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            waiting = {c.id for c in submissions.waiting(s, campaign)}
            for i, idea_id in self.ideas.items():
                idea = s.get_one(Idea, idea_id)
                assert idea.status is IdeaStatus(_at(state, 'idea', i)), f'idea {i}: {idea.status}'
                assert _shown(s, idea, waiting, self.bests) == SUBS[str(_at(state, 'sub', i))], f'idea {i}: submission'
            for r, run_id in self.runs.items():
                run = s.get_one(Run, run_id)
                held = {i for i, idea_id in self.ideas.items() if _holds(run, idea_id)}
                assert held == _ints(_at(state, 'held', r)), f'run {r}: holds {held}'
                assert bool(run.claims) == _at(state, 'claimed', r), f'run {r}: claims'
                assert bool(submissions.submitted(s, run_id)) == _at(state, 'submitted', r), f'run {r}: submitted'
                assert run.end == ENDS[str(_at(state, 'ended', r))], f'run {r}: {run.end}'
                base = self.based[r] if r in self.based else self.bests[_int(_at(state, 'base', r))]
                assert run.base_candidate_id == base, f'run {r}: from candidate#{run.base_candidate_id}'
            assert len(self.bests) - 1 == state['best'], 'the new bests'
            assert best_of(s, campaign) == s.get_one(Candidate, self.bests[-1]), 'the best'
            assert runs_stop.runs(s, campaign) == _holding(state), 'the places under max_runs'


def _shown(s: Session, idea: Idea, waiting: set[CandidateId], bests: list[CandidateId]) -> str:
    """What the code shows of the submission of the idea's newest claim: none, waiting, not the best, or the best."""
    if not idea.claims:
        return 'none'
    mine = submissions.submitted_for(s, idea.claims[-1])
    if not mine:
        return 'none'
    if mine[-1] in waiting:
        return 'waiting'
    return 'best' if mine[-1] in bests else 'not'


def _holds(run: Run, idea: IdeaId) -> bool:
    """Whether ``run`` holds ``idea`` now: the idea is claimed, and its newest claim is the run's."""
    claims = [c for c in run.claims if c.idea_id == idea]
    return bool(claims) and claims[0].idea.status is IdeaStatus.CLAIMED and claims[0].idea.claims[-1] is claims[0]


def _holding(state: State) -> int:
    """The model's ``Holding``: the runs that hold a place under ``max_runs``."""
    phases = _tuple(state['phase'])
    count = 0
    for r, phase in enumerate(phases, 1):
        attempt = _at(state, 'ended', r) not in ('stopped', 'restarted')
        counted = attempt and _at(state, 'submitted', r)
        held = bool(_ints(_at(state, 'held', r)))
        count += bool(counted or phase in ('running', 'wrapping', 'cut') or (attempt and held))
    return count


def _pair(value: Value) -> tuple[int, int]:
    first, second = _tuple(value)
    assert isinstance(first, int) and isinstance(second, int)
    return first, second


def _tuple(value: Value) -> tuple[Value, ...]:
    assert isinstance(value, tuple), value
    return value


def _int(value: Value) -> int:
    assert isinstance(value, int), value
    return value


def _ints(value: Value) -> set[int]:
    assert isinstance(value, frozenset), value
    return {_int(v) for v in value}


def fresh(db: Database, tmp_path: Path, name: str) -> Campaign:
    """A registered optimization campaign called ``name``, in a repo of its own, with its baseline measured."""
    path = tmp_path / name
    path.mkdir()
    (path / 'solve.txt').write_text('base\n')
    (path / 'data').mkdir()
    (path / '.gitignore').write_text('data\nworkspace\n')
    write_toml(path, HARNESS_APPEND, max_implementers=1)
    toml = path / 'piloop.toml'
    text = re.sub(r'(?m)^name = .*$', f'name = "{name}"', toml.read_text(), count=1)
    toml.write_text(text.replace('noise_runs = 1', f'noise_runs = {NOISE}', 1))  # a better score waits to be confirmed
    repo = Repo.init(path, initial_branch='main')
    repo.git.add('-A')
    repo.git.commit('-qm', 'baseline', env=commit_env(WORKER, WORKER))
    return register(db, path)


def test_claims_submissions_and_their_settle_move_as_the_model_on_every_transition_of_its_graph(
    db: Database, tmp_path: Path, runtime: ScriptRuntime
) -> None:
    traces = sorted(TRACES.glob('*.itf.json'))
    assert traces, 'no traces: see the module docstring'
    ex = controller(db, runtime, tmp_path)
    planner = OptimizationPlanner(TypeHost(Tracer(None), tmp_path, ex, at_once), Optimization())
    for seq, path in enumerate(traces):
        campaign = fresh(db, tmp_path, f'flush-{seq}')
        with db.session() as s:
            ideas = {}
            for i in (1, 2):
                idea = Idea(
                    campaign_id=campaign.id, track='score', title=f'idea {i}', hypothesis='h', created_by='test'
                )
                s.add(idea)
                s.flush()
                ideas[i] = idea.id
            s.commit()
            baseline = s.scalars(select(Candidate.id).where(Candidate.campaign_id == campaign.id)).one()
        replay = Replay(db, ex, planner, campaign, Path(campaign.path), ideas, [baseline])
        steps = _steps(path)
        replay.check(steps[0][1])
        for action, state in steps[1:]:
            if action == 'Done':
                continue
            try:
                replay.step(state)
            except AssertionError as e:
                pytest.fail(f'{path.name}, {action} {state["op"]}{state["arg"]}: {e}')
