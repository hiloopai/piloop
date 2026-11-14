"""Replay the state graph of the liveness model (``tests/models/liveness.tla``) against the real policy and rows.

TLC wrote the graph of ``liveness_graph.cfg`` (``-dump dot,actionlabels``), and ``scripts/graph_traces.py`` turned it
into traces that take every kind of transition that the driver checks, once at least (``--by
ag,run,idea,ends,claims,acq``; ``tests/traces/liveness``). Each trace has an idea of its own and two implementers, on a
clock that a tick moves by ``STEP``: the model's ``Q`` ticks are ``probes``' ``start_within`` and ``quiet_for``. Each
step runs the call that the model's action cites: a launch a running run and its ``starting`` row, an acquire a lease
taken at the clock's time, a run ``agent_session.running``, an output a ``SessionSaved``, a claim ``claim``, a run end
``end_run``, a cull and a recheck the policy's answer on ``cases`` (and ``end_agent`` with ``RunEnd.STALLED`` on an
``End``), a report the row's close and the run's end as ``run_outcome`` (or ``unstarted_end``) says, and a settle the
campaign type's ``settle``. After each step the rows, the run and the idea are the model's.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from piloop.actor import Agent, System
from piloop.agents.results import (
    AGENT_SESSION_STATES,
    AgentSessionRunning,
    SessionSaved,
    end_agent,
    finish,
)
from piloop.db.events import emit
from piloop.db.machine import transition
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Event, Lease, LeaseState
from piloop.db.session import Database
from piloop.ids import TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.ops import claim
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.controller import (
    RunFacts,
    _ended_by_daemon,
    close_run,
    run_outcome,
    unstarted_end,
)
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import end_requested, end_run
from piloop.plugins.campaign_type.kit.runs.stop import settle_claims
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.executor.contract import AGENT
from piloop.plugins.liveness.contract import Alive, EscalateStall, Stalled
from piloop.plugins.liveness.probes import Probes
from piloop.plugins.role.consumers import cases
from tests.conftest import baseline, home_cas
from tests.test_lease_model import State, _at, _steps

TRACES = Path(__file__).parent / 'traces' / 'liveness'
Q = 3
"""The model's ``Q``, as ``liveness_graph.cfg`` sets it."""
STEP = timedelta(minutes=5)
POLICY = Probes(start_within=Q * STEP, quiet_for=Q * STEP)
KILLED = 'implementer killed by signal 9'
"""The error of a process that the cull's kill ended."""
ROWS = {
    'starting': AgentSessionState.STARTING,
    'running': AgentSessionState.RUNNING,
    'ending': AgentSessionState.ENDING,
    'closed': AgentSessionState.KILLED,
}


@dataclass
class Replay:
    """One trace: its idea, the runs and rows of its two agents, the clock, and the agents culled before they ran."""

    db: Database
    campaign: Campaign
    idea: IdeaId
    now: datetime
    runs: dict[int, RunId] = field(default_factory=dict)
    rows: dict[int, int] = field(default_factory=dict)
    unstarted: set[int] = field(default_factory=set)

    def step(self, before: State, state: State) -> None:
        a = state['arg']
        assert isinstance(a, int)
        match state['op']:
            case 'launch':
                self._launch(a)
            case 'acquire':
                self._acquire(a)
            case 'running':
                with self.db.session() as s:
                    transition(
                        s,
                        AGENT_SESSION_STATES,
                        self.rows[a],
                        AgentSessionState.RUNNING,
                        AgentSessionRunning(machine=None),
                        System.DAEMON,
                    )
                    s.commit()
                self._at_now(AgentSessionRunning, a)
            case 'output':
                with self.db.session() as s:
                    saved = SessionSaved(session_file=home_cas(self.db).put(f'{a}@{self.now}'.encode()), session_id='s')
                    emit(s, saved, self._on(a), System.DAEMON)
                    s.commit()
                self._at_now(SessionSaved, a)
            case 'tick':
                self.now += STEP
            case 'claim':
                with self.db.session() as s:
                    claim(s, self.idea, actor=self._agent(a))
                    s.commit()
            case 'run_end':
                with self.db.session() as s:
                    end_run(s, RunEnding.GAVE_UP, 'stuck', actor=self._agent(a))
                    s.commit()
            case 'cull':
                answer = self._answer(a)
                if isinstance(answer, EscalateStall):
                    answer = answer.then
                assert isinstance(answer, Stalled), answer
                with self.db.session() as s:
                    w = s.get_one(AgentSession, self.rows[a])
                    if w.state is AgentSessionState.STARTING:
                        self.unstarted.add(a)
                    end_agent(s, w, answer.reason, RunEnd.STALLED, Tracer(None))
                    s.commit()
            case 'recheck':
                self._recheck(a, before)
            case 'report':
                self._report(a)
            case 'score':
                pass  # a run of the model submits nothing: its scoring waits for nothing
            case 'settle':
                with self.db.session() as s:
                    run = s.get_one(Run, self.runs[a])
                    run.scoring = False
                    assert run.end is not None
                    settle_claims(s, run, run.end)
                    s.commit()
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        self.check(state)

    def _on(self, a: int) -> TargetRef:
        return TargetRef(TargetKind.AGENT_SESSION, self.rows[a])

    def _agent(self, a: int) -> Agent:
        return Agent(role=IMPLEMENTER, session=self.rows[a])

    def _launch(self, a: int) -> None:
        """A free slot's run, and its implementer, which the pass took: ``starting``."""
        with self.db.session() as s:
            run = Run(
                campaign_id=self.campaign.id,
                base_candidate_id=baseline(self.db, self.campaign.id).id,
                harness='script',
                model='m',
                status=RunStatus.RUNNING,
                started_at=self.now,
            )
            s.add(run)
            s.flush()
            row = AgentSession(
                kind=IMPLEMENTER,
                campaign_id=self.campaign.id,
                subject=TargetRef(RUN, run.id),
                harness='script',
                model='m',
                state=AgentSessionState.STARTING,
                started_at=self.now,
            )
            s.add(row)
            s.commit()
            self.runs[a], self.rows[a] = RunId(run.id), row.id

    def _acquire(self, a: int) -> None:
        """A worker takes the agent's lease at the clock's time."""
        with self.db.session() as s:
            job = home_cas(self.db).put(f'agent {self.rows[a]}'.encode())
            s.add(
                Lease(
                    kind=AGENT,
                    agent_session_id=self.rows[a],
                    state=LeaseState.ACQUIRED,
                    worker='w',
                    job_cid=job,
                    acquired_at=self.now,
                    renewed_at=self.now,
                    expires_at=self.now + timedelta(hours=1),
                )
            )
            s.commit()

    def _at_now(self, kind: type[AgentSessionRunning | SessionSaved], a: int) -> None:
        """Date the agent's newest event of ``kind`` at the clock's time."""
        with self.db.session() as s:
            newest = s.scalars(
                select(Event.id)
                .where(Event.target == self._on(a), Event.kind.in_(kind.kinds()))
                .order_by(Event.id.desc())
            ).first()
            s.execute(update(Event).where(Event.id == newest).values(at=self.now))
            s.commit()

    def _answer(self, a: int) -> Alive | Stalled | EscalateStall:
        with self.db.session() as s:
            [case] = cases(s, [s.get_one(AgentSession, self.rows[a])], self.now)
        return POLICY.check(case)

    def _recheck(self, a: int, before: State) -> None:
        """The policy lets the agent go on, and asks again by when the model's clock would reach ``Q``."""
        answer = self._answer(a)
        assert isinstance(answer, Alive) and answer.recheck is not None, answer
        counting = _at(before, 'ag', a) == 'running' or _at(before, 'acq', a) is True
        quiet = _at(before, 'quiet', a)
        assert isinstance(quiet, int)
        assert answer.recheck == ((Q - quiet) * STEP if counting else POLICY.start_within), (answer, quiet)

    def _report(self, a: int) -> None:
        """The process ended: the row closes, then the run, as the controller ends it."""
        with self.db.session() as s:
            w = s.get_one(AgentSession, self.rows[a])
            killed = w.state is AgentSessionState.ENDING
            finish(s, w, AgentSessionState.KILLED, w.error)
            run = s.get_one(Run, self.runs[a])
            if a in self.unstarted:
                end, error = unstarted_end(s, run.id, w.id)
            else:
                facts = RunFacts(
                    ending=end_requested(s, run), ended=_ended_by_daemon(s, w.id), error=KILLED if killed else None
                )
                end, error = run_outcome(facts)
            close_run(s, run, end, error).scoring = True
            s.commit()

    def check(self, state: State) -> None:
        """The rows, the runs and the idea are the model's."""
        with self.db.session() as s:
            assert s.get_one(Idea, self.idea).status is IdeaStatus(_at_idea(state)), 'idea'
            for a in (1, 2):
                ag, run_state = _at(state, 'ag', a), _at(state, 'run', a)
                if ag == 'none':
                    assert a not in self.rows
                    continue
                w = s.get_one(AgentSession, self.rows[a])
                assert w.state is ROWS[str(ag)], (a, w.state, ag)
                run = s.get_one(Run, self.runs[a])
                ideas = [c.idea_id for c in run.claims]
                assert (self.idea in ideas) is _at(state, 'claims', a), (a, 'claims', ideas)
                match run_state:
                    case 'running':
                        assert run.status is RunStatus.RUNNING, (a, run.status)
                    case 'scoring' | 'closed':
                        assert run.scoring is (run_state == 'scoring'), (a, 'scoring')
                        assert run.end == RunEnd(str(_at(state, 'ends', a))), (a, run.end)
                    case other:
                        raise AssertionError(f'run {a}: {other}')


def _at_idea(state: State) -> str:
    idea = state['idea']
    assert isinstance(idea, str)
    return idea


def test_stuck_agents_end_as_the_liveness_model_on_every_transition_of_its_graph(
    db: Database, campaign: Campaign
) -> None:
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths, 'no traces: see the module docstring'
    now = datetime.now().astimezone()
    for seq, path in enumerate(paths):
        with db.session() as s:
            idea = Idea(track='score', campaign_id=campaign.id, title=f'idea {seq}', hypothesis='h', created_by='test')
            s.add(idea)
            s.commit()
            replay = Replay(db, campaign, IdeaId(idea.id), now)
        steps = _steps(path)
        for (_, before), (action, state) in zip(steps, steps[1:], strict=False):
            if action != 'Done':
                try:
                    replay.step(before, state)
                except AssertionError as e:
                    raise AssertionError(f'{path.name}, at {state["op"]}({state["arg"]}): {e}') from e
        now = replay.now + timedelta(days=1)
