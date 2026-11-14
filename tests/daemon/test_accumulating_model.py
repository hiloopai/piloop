"""Replay the state graphs of the accumulating model (``tests/models/accumulating.tla``) against the real trunk.

TLC wrote the graphs of ``accumulating_traces_bad.cfg`` and ``accumulating_traces_clash.cfg``, two runs each
(``-dump dot,actionlabels``), and
``scripts/graph_traces.py`` turned each into traces that take every kind of transition that the driver checks, once at
least (``--by sub,why,idea,trunk,ended,solo``; ``tests/traces/accumulating``). Each trace has a campaign of its own, a
git repo whose runs each work in a worktree, and the real evaluate command, which scores the lines of the ``*.txt``
files and fails on a file named ``bad``. Each step runs the code that the model's action cites: a submit
``submissions.submit``, an end ``close_run`` and then, once the run's submissions are decided, the type's ``settle``
(``RunController._settle`` waits for them), a batch ``trunk.start``, a finish ``RunController.measure_once`` then
``trunk.decide``, and a rebase a ``git merge -X ours`` of the trunk into the run's work, then a submit. After each step
every run's submission, idea and refusal, the trunk and the submissions that go alone are the model's.
"""

import re
import subprocess
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from git import Repo
from sqlalchemy.orm import Session

from piloop.db.events import latest
from piloop.db.models import Campaign, Event
from piloop.db.session import Database
from piloop.ids import Revision, TargetRef
from piloop.plugins.campaign_type.accumulating import ACCUMULATING
from piloop.plugins.campaign_type.accumulating.trunk import Batch, alone, decide, head, merged, start
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.evaluation import submissions
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import Measuring, RunController, close_run
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunId
from piloop.plugins.campaign_type.kit.runs.stop import settle_claims
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.vcs.git import Git
from piloop.plugins.vcs.git.commands import Identity, commit_env
from tests.conftest import HARNESS_APPEND, ScriptRuntime, register, reports, write_toml
from tests.daemon.test_pool import controller, running, submitted
from tests.test_lease_model import State, Value, _at, _steps

TRACES = Path(__file__).parent.parent / 'traces' / 'accumulating'
GRAPHS = {'bad': ({2}, []), 'clash': (set(), [{1, 2}])}
"""Each graph's constants, as its ``.cfg`` sets them: the runs whose work fails the check, and the pairs that clash."""
EVALUATE = ['sh', '-c', f'test -e bad && exit 3; {reports(score="$(cat *.txt | wc -l)", size="1")}']
"""Score the lines of every ``*.txt``, and fail on a file ``bad``."""
WORKER = Identity(name='worker', email='worker@localhost')


@dataclass
class Replay:
    """One trace's campaign, its runs' worktrees, and the merge in flight."""

    db: Database
    ex: RunController
    campaign: Campaign
    bad: set[int]
    clash: list[set[int]]
    runs: dict[int, RunId]
    worktrees: dict[int, Path]
    batch: Batch | None = None
    settled: set[int] = field(default_factory=set)

    def step(self, state: State) -> None:
        runs = _runs(state['arg'])
        match state['op']:
            case 'submit':
                [r] = runs
                self._submit(r, self._work(r))
            case 'rebase':
                [r] = runs
                self._submit(r, self._rebased(r))
            case 'end':
                [r] = runs
                with self.db.session() as s:
                    close_run(s, s.get_one(Run, self.runs[r]), RunEnd.FINISHED, None)
                    s.commit()
            case 'batch':
                self._start(runs)
            case 'finish':
                assert self.batch is not None and set(self.batch.members) == self._latest(runs)
                assert self.ex.measure_once(self.batch.merge) is Measuring.MEASURED
                with self.db.session() as s:
                    decide(s, self.batch)
                    s.commit()
                self.batch = None
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        self._settle()
        self.check(state)

    def _work(self, r: int) -> Revision:
        """Run ``r``'s work: a file of its own, its line of ``solve.txt`` if it clashes, and ``bad`` if it fails.

        The last run of a clashing pair writes only its line: a merge of it into the other's work keeps that one's tree.
        """
        path = self.worktrees[r]
        if not any(r == max(pair) for pair in self.clash):
            (path / f'work{r}.txt').write_text(f'run {r}\n')
        if any(r in pair for pair in self.clash):
            (path / 'solve.txt').write_text(f'run {r}\n')
        if r in self.bad:
            (path / 'bad').write_text('')
        return self._commit(path, f'the work of run {r}')

    def _rebased(self, r: int) -> Revision:
        """Merge the trunk into run ``r``'s work, its own side winning each conflict, as its worker is told to."""
        with self.db.session() as s:
            trunk = head(s, s.get_one(Campaign, self.campaign.id))
            assert trunk is not None
            revision = trunk.revision
        repo = Repo(self.worktrees[r])
        repo.git.merge('-q', '-X', 'ours', '--no-edit', revision, env=commit_env(WORKER, WORKER))
        return Revision(repo.head.commit.hexsha)

    def _commit(self, path: Path, subject: str) -> Revision:
        repo = Repo(path)
        repo.git.add('-A')
        repo.git.commit('-qm', subject, env=commit_env(WORKER, WORKER))
        return Revision(repo.head.commit.hexsha)

    def _submit(self, r: int, revision: Revision) -> None:
        submitted(self.db, self.campaign, self.runs[r], revision)

    def _start(self, runs: frozenset[int]) -> None:
        with self.db.session() as s, closing(Git().open(Path(self.campaign.path))) as repo:
            self.batch = start(s, repo, s.get_one(Campaign, self.campaign.id))
            s.commit()
        assert (set() if self.batch is None else set(self.batch.members)) == self._latest(runs)

    def _latest(self, runs: frozenset[int]) -> set[CandidateId]:
        """The newest submission of each run of ``runs``."""
        with self.db.session() as s:
            return {submissions.submitted(s, self.runs[r])[-1] for r in runs}

    def _settle(self) -> None:
        """Settle each ended run once its submissions are decided, as ``RunController._settle`` waits to."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            waiting = {c.id for c in submissions.waiting(s, campaign)}
            for r, run_id in self.runs.items():
                run = s.get_one(Run, run_id)
                if r in self.settled or run.end is None or not waiting.isdisjoint(submissions.submitted(s, run_id)):
                    continue
                settle_claims(s, run, run.end)
                self.settled.add(r)
            s.commit()

    def check(self, state: State) -> None:
        """The code's state is the model's: each run's submission, idea, refusal and solitude, and the trunk."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            done = merged(s, campaign)
            waiting = {c.id: c for c in submissions.waiting(s, campaign)}
            in_flight = set() if self.batch is None else set(self.batch.members)
            trunk, solo = set(), set()
            for r, run_id in self.runs.items():
                mine = submissions.submitted(s, run_id)
                latest = mine[-1] if mine else None
                if latest is None:
                    sub = 'none'
                elif latest in in_flight:
                    sub = 'checking'
                elif latest in done:
                    sub = 'merged'
                elif latest in waiting:
                    sub = 'waiting'
                    solo |= {r} if alone(s, waiting[latest]) else set()
                else:
                    sub = 'refused'
                if done.intersection(mine):
                    trunk.add(r)
                run = s.get_one(Run, run_id)
                idea = s.get_one(Idea, run.claims[0].idea_id).status
                assert sub == _field(state, 'sub', r), f'run {r}: submission {sub}'
                assert idea == IdeaStatus(_field(state, 'idea', r)), f'run {r}: idea {idea}'
                assert (run.end is not None) == _field(state, 'ended', r), f'run {r}: ended {run.end}'
                if sub == 'refused':
                    assert latest is not None
                    told = _told(s, latest)
                    why = 'conflict' if 'conflicts with the trunk' in told else 'check'
                    assert why == _field(state, 'why', r), f'run {r}: refused, {told}'
            assert trunk == _runs(state['trunk']), f'trunk {trunk}'
            model_solo = {r for r in _runs(state['solo']) if _field(state, 'sub', r) == 'waiting'}
            assert solo == model_solo, f'alone {solo}'


def _field(state: State, name: str, r: int) -> Value:
    """``state.name[r]``: TLC writes a function of the runs ``1..n`` as a sequence."""
    return _at(state, name, r)


def _tuple(value: Value) -> tuple[Value, ...]:
    assert isinstance(value, tuple), value
    return value


def _runs(value: Value) -> frozenset[int]:
    """A set of runs of the model."""
    assert isinstance(value, frozenset), value
    found = frozenset(r for r in value if isinstance(r, int))
    assert len(found) == len(value), value
    return found


def _told(s: Session, candidate: CandidateId) -> str:
    found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate))
    assert found is not None
    return found[1].told


def fresh(db: Database, tmp_path: Path, name: str) -> Campaign:
    """A registered accumulating campaign called ``name``, in a repo of its own, with its baseline measured."""
    path = tmp_path / name
    path.mkdir()
    (path / 'solve.txt').write_text('base\n')
    (path / 'data').mkdir()
    (path / '.gitignore').write_text('data\nworkspace\n')
    write_toml(path, HARNESS_APPEND, max_implementers=3, evaluate=EVALUATE)
    toml = path / 'piloop.toml'
    toml.write_text(
        f'type = "{ACCUMULATING}"\n' + re.sub(r'(?m)^name = .*$', f'name = "{name}"', toml.read_text(), count=1)
    )
    repo = Repo.init(path, initial_branch='main')
    repo.git.add('-A')
    repo.git.commit('-qm', 'baseline', env=commit_env(WORKER, WORKER))
    return register(db, path)


@pytest.mark.parametrize('graph', sorted(GRAPHS))
def test_the_trunk_moves_as_the_model_on_every_transition_of_its_graph(
    db: Database, tmp_path: Path, runtime: ScriptRuntime, graph: str
) -> None:
    bad, clash = GRAPHS[graph]
    ex = controller(db, runtime, tmp_path)
    traces = sorted((TRACES / graph).glob('*.itf.json'))
    assert traces, f'no traces of {graph}: see the module docstring'
    for seq, path in enumerate(traces):
        campaign = fresh(db, tmp_path, f'{graph}-{seq}')
        with db.session() as s:
            assert s.get_one(Campaign, campaign.id).type == ACCUMULATING
        steps = _steps(path)
        runs = {r: running(db, campaign) for r in range(1, len(_tuple(steps[0][1]['sub'])) + 1)}
        worktrees = {}
        for r in runs:
            worktrees[r] = tmp_path / f'{graph}-{seq}-r{r}'
            subprocess.run(
                ['git', '-C', campaign.path, 'worktree', 'add', '-q', '--detach', str(worktrees[r])], check=True
            )
        replay = Replay(db, ex, campaign, bad, clash, runs, worktrees)
        replay.check(steps[0][1])
        for action, state in steps[1:]:
            if action != 'Done':
                try:
                    replay.step(state)
                except AssertionError as e:
                    raise AssertionError(f'{graph}/{path.name}, at {action}: {e}') from e
