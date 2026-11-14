"""The liveness capability: the ``probes`` policy's answers, and how the daemon ends the agents that it ends."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from piloop.config import load_campaign_config
from piloop.daemon.scheduler import Scheduler
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, utcnow
from piloop.db.session import Database
from piloop.ids import RoleName, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd, RunStatus
from piloop.plugins.campaign_type.kit.runs.stop import runs
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.host import Tier, data_errors
from piloop.plugins.liveness.contract import LIVENESS, AgentCase, Alive, EscalateStall, LivenessPolicy, Stalled
from piloop.plugins.liveness.probes import Probes
from tests.conftest import (
    HARNESS_APPEND,
    HARNESS_SLEEPS,
    ScriptRuntime,
    has_lines,
    run_all,
    settle,
    wait_until,
    write_toml,
)
from tests.daemon.test_scheduler import add_ideas, make_scheduler, set_config
from tests.reports.test_overview import render

MIN = timedelta(minutes=1)


def case(
    state: AgentSessionState = AgentSessionState.RUNNING,
    running: timedelta | None = None,
    quiet: timedelta | None = None,
    stalls: int = 0,
) -> AgentCase:
    return AgentCase(RoleName('implementer'), 'pi', state, running, quiet, stalls)


STARTING, RUNNING = AgentSessionState.STARTING, AgentSessionState.RUNNING


@pytest.mark.parametrize(
    ('agent', 'answer'),
    [
        pytest.param(case(STARTING), Alive(15 * MIN), id='waits-for-a-worker'),
        pytest.param(case(STARTING, running=5 * MIN), Alive(10 * MIN), id='starting'),
        pytest.param(case(STARTING, running=15 * MIN), Stalled('it did not start within 15 min'), id='did-not-start'),
        pytest.param(case(running=30 * MIN, quiet=5 * MIN), Alive(15 * MIN), id='writes'),
        pytest.param(case(running=40 * MIN, quiet=21 * MIN), Stalled('no output for 21 min'), id='quiet'),
        pytest.param(case(running=40 * MIN), Alive(20 * MIN), id='no-snapshot-yet'),
        pytest.param(case(quiet=timedelta(seconds=90)), Alive(timedelta(seconds=1110)), id='under-two-minutes'),
        pytest.param(
            case(running=40 * MIN, quiet=20 * MIN, stalls=1),
            EscalateStall(
                'stall 2 of its idea: no output for 20 min; the idea stays open', then=Stalled('no output for 20 min')
            ),
            id='second-stall',
        ),
        pytest.param(
            case(running=40 * MIN, quiet=20 * MIN, stalls=2), Stalled('no output for 20 min'), id='third-stall'
        ),
        pytest.param(case(running=40 * MIN, quiet=5 * MIN, stalls=1), Alive(15 * MIN), id='live-after-a-stall'),
    ],
)
def test_probes_end_an_agent_that_does_not_start_or_goes_quiet_and_escalate_the_second_stall_of_an_idea(
    agent: AgentCase, answer: Alive | Stalled | EscalateStall
) -> None:
    assert Probes().check(agent) == answer


def test_the_policy_is_pure_one_per_campaign_and_probes_by_default(tmp_path: Path) -> None:
    assert LIVENESS.tier is Tier.PURE and data_errors(LivenessPolicy) == []
    assert LIVENESS.selected({}) == Probes()
    write_toml(tmp_path, HARNESS_APPEND)
    cfg = load_campaign_config(tmp_path, plugins={'liveness': {'quiet_for': 'PT45M', 'escalate_at': 3}})
    assert LIVENESS.selected(cfg.plugins) == Probes(quiet_for=45 * MIN, escalate_at=3)


QUIET = {'liveness': {'quiet_for': 'PT4S'}}


def stalled_run(db: Database, scheduler: Scheduler, run: int) -> None:
    """Pass until the policy ends the agent of ``run``, quiet since it started running. Then its run settles."""
    wait_until(lambda: _state(db, run) is AgentSessionState.RUNNING)
    run_all(scheduler)
    assert _state(db, run) is AgentSessionState.RUNNING, 'quiet for less than quiet_for'

    def ended() -> bool:
        run_all(scheduler)
        return _state(db, run) is not AgentSessionState.RUNNING

    wait_until(ended)
    settle(scheduler, db)


def _state(db: Database, run: int) -> AgentSessionState:
    with db.session() as s:
        return s.scalars(select(AgentSession.state).where(AgentSession.subject == TargetRef(RUN, run))).one()


def _ended(db: Database, run: int) -> tuple[RunStatus, RunEnd | None, str]:
    with db.session() as s:
        found = s.get_one(Run, run)
        return found.status, found.end, re.sub(r'\d+ s$', 'N s', found.error or '')


def test_a_quiet_implementer_ends_its_run_stalled_frees_its_slot_and_its_ideas_second_stall_escalates_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A harness that hangs and writes nothing holds its slot until ``quiet_for``, not until its role timeout."""
    write_toml(Path(campaign.path), HARNESS_SLEEPS)
    set_config(db, campaign, plugins=QUIET)
    (idea,) = add_ideas(db, campaign, 'hangs')
    scheduler = make_scheduler(db, runtime, tmp_path)
    assert run_all(scheduler) == [1]
    stalled_run(db, scheduler, 1)
    assert _ended(db, 1) == (RunStatus.FAILED, RunEnd.STALLED, 'no output for N s')
    with db.session() as s:
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        [note] = s.scalars(select(Comment.body).where(Comment.target == TargetRef(IDEA, idea))).all()
        assert note.startswith('open: run#1 gave the idea back; it ended stalled: no output for ')
        assert runs(s, s.get_one(Campaign, campaign.id)) == 0, 'no attempt: max_runs does not count it'
        assert s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all() == []
    status = re.sub(r'\d+ s, \d+s ago', 'N s, Ns ago', render(db, campaign, now=utcnow()))
    has_lines(status, '- Ended stalled in the last hour (`[plugins.liveness]`): 1')
    has_lines(status, '  - implementer#1 ended stalled: no output for N s, Ns ago')

    assert run_all(scheduler) == [2], 'the slot is free: the next pass starts a run, which claims the idea again'
    stalled_run(db, scheduler, 2)
    assert _ended(db, 2) == (RunStatus.FAILED, RunEnd.STALLED, 'no output for N s')
    with db.session() as s:
        assert s.get_one(Idea, idea).status is IdeaStatus.OPEN
        [escalation] = s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all()
        assert (escalation.target, escalation.priority) == (TargetRef(RUN, 2), 0)
        assert 'Liveness policy probes: stall 2 of its idea: no output for ' in escalation.body
    scheduler.stop(timeout=5)
