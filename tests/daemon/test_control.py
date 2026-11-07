"""The daemon's side of the control operations: a pause holds the tick, and a skipped commit is measured on request."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.campaign import ops
from piloop.db import feed as db_feed
from piloop.db.events import latest
from piloop.db.models import ENDED, AgentSession, AgentSessionState, Campaign, Comment, Event
from piloop.db.session import Database
from piloop.ids import CampaignId, CampaignStatus, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, Origin
from piloop.plugins.campaign_type.kit.candidates.references import import_reference
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import remeasure_asked, request_remeasure
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner
from piloop.plugins.vcs.git import PILOOP, GitRepository
from tests.conftest import SETTLE, ScriptRuntime, run_all, write_script
from tests.daemon.test_run_controller import controller, execute, make_run
from tests.daemon.test_scheduler import add_ideas, make_scheduler, measured_by, request_auditor, statuses

pytestmark = pytest.mark.usefixtures('auditor')


def pause(db: Database, *, evals: bool) -> None:
    with db.session() as s:
        ops.pause(s, CampaignId(1), System.HUMAN, evals=evals, reason='wait for the new evaluator')
        s.commit()


def test_a_paused_campaign_launches_nothing_requests_nothing_and_escalates_nothing_until_resumed(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    sched = make_scheduler(db, runtime, tmp_path)
    add_ideas(db, campaign, 'more lines')
    asked = request_auditor(db, campaign)
    pause(db, evals=False)
    assert run_all(sched) == [] and run_all(sched) == []
    assert statuses(db) == [IdeaStatus.OPEN], 'no run is queued'
    with db.session() as s:
        agent_sessions = s.scalars(select(AgentSession)).all()
        assert [(w.id, w.state) for w in agent_sessions] == [(asked, AgentSessionState.REQUESTED)], (
            'nothing launches or is asked'
        )
        assert s.scalars(select(Comment)).all() == [], 'a pause raises no escalation'
        ops.resume(s, CampaignId(1))
        s.commit()
    launched = run_all(sched)
    assert asked in launched and len(launched) == 2, 'the request and the run of the idea'
    assert statuses(db) == [IdeaStatus.CLAIMED]
    sched.stop(timeout=10)


def test_a_pause_still_measures_what_the_evaluator_needs_unless_it_holds_the_evaluations_too(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    repo = Repo(campaign.path)
    repo.git.checkout('-qb', 'elsewhere')
    (Path(campaign.path) / 'solve.txt').write_text('a\nb\nc\n')
    repo.git.commit('-qam', 'three lines')
    repo.git.checkout('-q', 'main')
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        reference = import_reference(
            s, proj, GitRepository(repo, PILOOP), 'elsewhere', summary=None, actor=System.ORCHESTRATOR
        ).id
        s.commit()
    sched = make_scheduler(db, runtime, tmp_path)
    pause(db, evals=True)
    run_all(sched)
    sched.wait(SETTLE)
    assert measured_by(db, reference) == [], 'held'
    pause(db, evals=False)
    run_all(sched)
    sched.wait(SETTLE)
    assert measured_by(db, reference) == [(1, 3.0, None)], 'a pause of the launches only still measures'
    sched.stop(timeout=5)


SKIP = '#!/bin/sh\necho a >> solve.txt\ngit commit -qam scaffold -m "Piloop-Eval: skip"\n'
"""A run whose one commit is marked skip, so the daemon never measures it."""


def test_a_skipped_commit_is_measured_on_request_and_enters_the_pool(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """``candidate remeasure`` measures a skipped commit, to isolate a regression to it: it is a run's candidate then."""
    write_script(Path(campaign.path).parent / 'harness.sh', SKIP)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        scaffold = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).one()
        request_remeasure(s, scaffold, System.ORCHESTRATOR, repeats=1)
        s.commit()
        scaffold_id = scaffold.id
    pause(db, evals=False)  # no new run of the idea: its commit of the same tree would be scored
    sched = make_scheduler(db, runtime, tmp_path)
    run_all(sched)
    sched.wait(SETTLE)
    with db.session() as s:
        again = s.get_one(Candidate, scaffold_id)
        assert (again.origin, len(again.measurements)) == (Origin.RUN, 1)
        found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, scaffold_id))
        assert found is not None and found[1].best == scaffold_id, 'scored, and it beats the baseline'
        assert remeasure_asked(s, again) is None, 'answered'
    sched.stop(timeout=5)


def test_an_agent_requested_while_a_retire_checks_either_stops_the_retire_or_never_lands(
    db: Database, campaign: Campaign, mocker: MockerFixture
) -> None:
    """A retire and a launch race: the retire must never pass while an agent of its campaign is open."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        s.commit()
    path = db.engine.url.database
    assert path is not None
    landed: list[bool] = []

    def scheduler_requests(*_: object) -> bool:
        """The daemon's pass commits a request on its own connection, between the retire's check and its change."""
        with closing(sqlite3.connect(path, timeout=0.2, isolation_level=None)) as con:
            try:
                con.execute("INSERT INTO agent_sessions (kind, campaign_id) VALUES ('auditor', ?)", (campaign.id,))
                landed.append(True)
            except sqlite3.OperationalError as e:
                assert 'database is locked' in str(e), e
                landed.append(False)
        return False

    mocker.patch.object(ops, 'unclosed', side_effect=scheduler_requests)
    with db.session() as s:
        try:
            ops.retire(s, campaign.id, System.HUMAN, reason='done')
            s.commit()
        except ops.Refused:
            s.rollback()
    with db.session() as s:
        retired = s.get_one(Campaign, campaign.id).status is CampaignStatus.RETIRED
        open_ = s.scalars(select(AgentSession.id).where(AgentSession.state.not_in(ENDED))).all()
    assert landed and not (retired and open_), (landed, retired, open_)


def test_a_campaign_retired_while_the_daemon_takes_its_request_launches_nothing(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The second guard: a pass that read the campaign before a retire committed never launches its rows."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        s.commit()
    asked = request_auditor(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    path = db.engine.url.database
    assert path is not None
    remeasure = OptimizationPlanner.remeasure

    def retired_first(planner: OptimizationPlanner, s: Session, read: Campaign) -> None:
        """The retire commits on its own connection, after the pass read the campaign and before it writes."""
        with closing(sqlite3.connect(path, timeout=0.2, isolation_level=None)) as con:
            con.execute("UPDATE campaigns SET status = 'retired' WHERE id = ?", (read.id,))
        remeasure(planner, s, read)

    mocker.patch.object(OptimizationPlanner, 'remeasure', autospec=True, side_effect=retired_first)
    assert run_all(scheduler) == []
    with db.session() as s:
        row = s.get_one(AgentSession, asked)
        assert (row.state, row.error) == (AgentSessionState.KILLED, 'its campaign is retired')


def test_a_campaign_that_a_feed_read_finds_retired_during_the_pass_drops_out_of_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A feed read (a role's, say) reloads what the pass read: a retire that committed before it shows."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by the test'
        s.commit()
    asked = request_auditor(db, campaign)
    scheduler = make_scheduler(db, runtime, tmp_path)
    path = db.engine.url.database
    assert path is not None
    remeasure = OptimizationPlanner.remeasure

    def retired_then_read(planner: OptimizationPlanner, s: Session, read: Campaign) -> None:
        """The retire commits on its own connection, then the pass reads the feed, before its quiet phase."""
        with closing(sqlite3.connect(path, timeout=0.2, isolation_level=None)) as con:
            con.execute("UPDATE campaigns SET status = 'retired' WHERE id = ?", (read.id,))
        db_feed.after(s, 0, limit=1)
        remeasure(planner, s, read)

    mocker.patch.object(OptimizationPlanner, 'remeasure', autospec=True, side_effect=retired_then_read)
    assert run_all(scheduler) == []
    with db.session() as s:
        assert s.get_one(AgentSession, asked).state is AgentSessionState.REQUESTED, 'the pass left it alone'
