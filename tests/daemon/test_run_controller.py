import fcntl
import json
import os
import py_compile
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import DEFAULT

import pytest
from dirty_equals import IsPositive, IsStr
from git import GitCommandError, Repo
from inline_snapshot import snapshot
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pytest_mock import MockerFixture
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.agents.launcher import Launcher
from piloop.agents.results import (
    Ended,
    Lost,
    NotStarted,
    StartupAnomaly,
    WorkUnsaved,
    never_worked,
)
from piloop.agents.roles import PILOOP_HINT, read_brief
from piloop.campaign import ops, stop
from piloop.campaign.campaigns import sync
from piloop.cli.plugins import selections
from piloop.config import CampaignConfig, ConfigError, load_campaign_config
from piloop.db.events import has, in_order, latest
from piloop.db.machine import Refused
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import OriginChanged
from piloop.ids import CampaignId, CampaignStatus, CommentKind, Ending, Revision, TargetRef
from piloop.otel import Span, Tracer
from piloop.plugins import host
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import jobs as evaluation_jobs, queue
from piloop.plugins.campaign_type.kit.evaluation.evaluate import CHANGED_INPUTS
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.evaluation.jobs import INPUTS_CHANGED
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    current_evaluator,
    request_remeasure,
    requested,
    save_units,
    units,
)
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator, Measurement
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BEST_RULES
from piloop.plugins.campaign_type.kit.pool.due import due, unready
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, best_of, track_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs import stop as runs_stop
from piloop.plugins.campaign_type.kit.runs.controller import (
    UNSAVED,
    Measuring,
    RunController,
    RunFacts,
    RunFinished,
    RunRelaunched,
    RunStarted,
    close_run,
    run_outcome,
)
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import RunEndRequested
from piloop.plugins.campaign_type.kit.runs.section.long_jobs import LongJobs
from piloop.plugins.campaign_type.kit.runs.stop import release
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of, working_on
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.executor.contract import Source
from piloop.plugins.executor.kit import jobs
from piloop.plugins.executor.kit.jobs import INPUTS_REBUILT
from piloop.plugins.executor.workers import Workers
from piloop.plugins.executor.workers.worker import _Control
from piloop.plugins.harness.contract import cleared_env
from piloop.plugins.harness.kit.jsonl import dash_separators_wrapped
from piloop.plugins.memory.local.tables import Lesson, LessonStatus
from piloop.plugins.runtime.contract import AgentExit, Deadline, Handle
from piloop.plugins.vcs.contract import Part
from piloop.plugins.vcs.git import PILOOP, UNCOMMITTED, Git, GitRepository, commands as git
from piloop.plugins.vcs.git.commands import pin as git_pin
from piloop.settings import AgentSettings, Settings
from piloop.store.tree import FICLONE
from tests.conftest import (
    END_RUN,
    HARNESS_APPEND,
    HARNESS_COMMITS_AND_LEAVES_DIRT,
    HARNESS_FORGETS_COMMIT,
    HARNESS_NOOP,
    HARNESS_PROMPT_TO_FILE,
    HARNESS_SLEEPS,
    HARNESS_TRY_AND_REVERT,
    HARNESS_TWO_COMMITS,
    HARNESS_WORSE,
    Gate,
    ScriptRuntime,
    baseline,
    escalation_sql,
    events,
    example,
    factory,
    home_cas,
    pinned,
    reports,
    serving,
    span_named,
    wait_until,
    workers_of,
    write_toml,
)
from tests.test_git import by
from tests.test_vcs import git_words
from tests.usage.test_usage import READS_PI, SESSIONS


def make_run(
    db: Database,
    campaign: Campaign,
    *,
    title: str = 'more lines',
    plan: str | None = None,
    provenance: str | None = None,
    track: str = 'score',
) -> RunId:
    base = baseline(db, campaign.id).id  # before the session: one thread never holds two connections
    with db.session() as s:
        idea = Idea(
            track=track,
            campaign_id=campaign.id,
            title=title,
            hypothesis='lines are good',
            plan=plan,
            provenance=provenance,
            created_by='test',
            status=IdeaStatus.CLAIMED,
        )
        s.add(idea)
        s.flush()
        run = Run(
            campaign_id=idea.campaign_id,
            idea_id=idea.id,
            attempt=1,
            base_candidate_id=base,
            harness='script',
            model='test-model',
        )
        s.add(run)
        s.flush()
        s.add(
            AgentSession(
                kind=IMPLEMENTER,
                campaign_id=campaign.id,
                subject=TargetRef(RUN, run.id),
                harness='script',
                model='test-model',
                reason=f'idea#{idea.id} is ready',
            )
        )
        s.commit()
        return run.id


def execute(ex: RunController, db: Database, run_id: RunId) -> None:
    """Run ``run_id`` through its requested agent session row, the way the scheduler does: it takes the row, then executes."""
    with db.session() as s:
        requested = select(AgentSession).where(working_on(run_id), AgentSession.state == AgentSessionState.REQUESTED)
        agent_session = s.scalars(requested).one()
        agent_session.state = AgentSessionState.STARTING
        s.commit()
        agent_session_id = agent_session.id
    ex.execute(run_id, agent_session_id)


def controller(
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer | None = None,
    agents: AgentSettings | None = None,
) -> RunController:
    return serving(
        RunController(db, tracer or Tracer(None), tmp_path, factory(runtime), executor=Workers(), agents=agents)
    )


WORKED = AgentSettings(startup_seconds=0)
"""An implementer that ends at once has worked: it is scored by what it left."""


def test_an_implementer_whose_launch_the_stop_rule_stops_never_starts_and_its_run_stays_queued(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The rule answers ``Stop`` (the best reached the target) after the pass took the run: nothing starts. The run
    stays queued and its implementer is requested again, for the pass that stops the campaign to close both."""
    run_id = make_run(db, campaign)
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'target': -1e9}}
        )
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        [agent] = s.scalars(select(AgentSession).where(working_on(run_id))).all()
        assert (run.status, run.end, run.brief_cid, agent.state, agent.session_id) == (
            RunStatus.QUEUED,
            None,
            None,
            AgentSessionState.REQUESTED,
            None,
        )


def reload_campaign(db: Database, campaign: Campaign) -> None:

    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path))
        s.commit()


def told_of(s: Session, candidate: CandidateId) -> str | None:
    """What the candidate's last score told its worker (``CandidateScored.told``), or None before it is scored."""
    found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate))
    return found[1].told if found is not None else None


def is_best(s: Session, c: Candidate) -> bool:
    """Whether ``c`` is the best of its run's track now."""
    campaign = s.get_one(Campaign, c.campaign_id)
    return Pool.of(s, campaign, track_of(campaign.config, c)).best() == c.id


class Scored:
    """A candidate with its (single) measurement and where it stands flattened on, for terse assertions."""

    def __init__(self, s: Session, c: Candidate) -> None:
        self.id, self.parent_id, self.revision, self.summary = c.id, c.parent_id, c.revision, c.summary
        self.told, self.best = told_of(s, c.id), is_best(s, c)
        [m] = c.measurements
        self.metrics, self.error = m.metrics, m.error


def candidates(db: Database, run_id: RunId) -> list[Scored]:
    """The candidates a run produced (its commits' trees), with their one measurement each."""
    with db.session() as s:
        rows = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        return [Scored(s, c) for c in rows]


def candidates_measured(db: Database, run_id: RunId) -> list[Scored]:
    """``candidates``, without the ones nothing measured (a skip commit, recovered work)."""
    with db.session() as s:
        rows = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        return [Scored(s, c) for c in rows if c.measurements]


def brief_of(db: Database, run_id: RunId) -> str:
    """The text of the run's brief, from the home's CAS, or '' before the run starts."""
    brief = run_row(db, run_id).brief_cid
    return read_brief(home_cas(db), brief) if brief is not None else ''


def run_row(db: Database, run_id: RunId) -> Run:
    with db.session() as s:
        return s.get_one(Run, run_id)


def idea_of(run: Run) -> Idea:
    """The idea that ``run`` claimed: each run of these tests claims one (``make_run``)."""
    assert run.idea is not None, f'run#{run.id} claimed an idea'
    return run.idea


def claimed_id(run: Run) -> IdeaId:
    """The id of the idea that ``run`` claimed."""
    assert run.idea_id is not None, f'run#{run.id} claimed an idea'
    return run.idea_id


@pytest.mark.usefixtures('submitting')
def test_successful_run_scores_the_commit(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    run_id = make_run(db, campaign, plan='append one line', provenance='finding comment#3: short files lose')
    execute(controller(db, runtime, tmp_path, tracer), db, run_id)
    agent, measure, candidate, run_span = [s for s in spans.get_finished_spans() if s.name != 'piloop.job']
    assert dict(agent.attributes or {}) == snapshot(
        {
            'agent_session': 1,
            'kind': 'implementer',
            'harness': 'script',
            'model': 'test-model',
            'reason': 'idea#1 is ready',
            'state': 'exited',
        }
    )
    assert dict(run_span.attributes or {}) == snapshot(
        {
            'run': 1,
            'agent_session': 1,
            'harness': 'script',
            'model': 'test-model',
            'status': 'succeeded',
            'end': 'finished',
        }
    )
    assert candidate.parent is not None and candidate.parent.span_id == run_span.context.span_id
    assert measure.parent is not None and measure.parent.span_id == candidate.context.span_id
    assert dict(measure.attributes or {}) == snapshot(
        {
            'run': 1,
            'revision': IsStr(),
            'candidate': 2,
            'evaluation': 1,
            'joined': False,
            'measurement': 2,
            'metric.score': 2.0,
            'metric.size': IsPositive(),
        }
    )
    assert dict(candidate.attributes or {}) == snapshot(
        {'run': 1, 'revision': IsStr(), 'candidate': 2, 'status': 'measured'}
    )

    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    brief = brief_of(db, run_id)
    assert brief.startswith('# Implementer') and 'append one line' not in brief, 'the idea comes with the claim'
    assert 'lines are good' not in brief and 'short files lose' not in brief
    [e] = candidates(db, run_id)
    assert (e.best, e.summary, e.metrics) == (True, 'add a line', {'score': 2.0, 'size': IsPositive()})
    assert e.told == snapshot('score 2 over 1 eval: the best of the score track now, rank 1 of 2; vs parent +1')
    assert e.parent_id == baseline(db, campaign.id).id
    with db.session() as s:
        assert s.get_one(Idea, run.idea_id).status is IdeaStatus.DONE
        [agent_session] = s.scalars(select(AgentSession)).all()
        assert (agent_session.state, run_of(agent_session)) == (AgentSessionState.EXITED, run_id)
        kinds = [ev.kind for ev in s.scalars(select(Event)).all()]
    assert kinds == snapshot(
        [
            'idea.created',
            'agent_session.requested',
            'lease_worker.joined',
            'run.started',
            'agent_session.started',
            'lease.queued',
            'agent_session.running',
            'run.end_requested',
            'agent_session.finished',
            'agent_session.job_ended',
            'run.received',
            'run.finished',
            'lease.queued',
            'measurement.recorded',
            'candidate.scored',
            'idea.status',
            'subject.settled',
        ]
    ), 'the run closes when its implementer ends; the idea settles once the commit is scored'

    repo = Repo(campaign.path)
    assert [r.path for r in repo.refs if r.path.startswith('refs/piloop/')] == [f'refs/piloop/demo/r{run_id}/0']
    assert repo.head.commit.summary == 'baseline', 'the campaign checkout never moves'
    log_dir = tmp_path / 'runs' / str(run_id)
    assert (
        (log_dir / 'brief.md').is_file()
        and (log_dir / 'transcript.txt').is_file()
        and not (log_dir / 'worktree').exists()
    )
    assert runtime.killed, 'the runtime is told to release the implementer'


HARNESS_WRITES_THE_WORKSPACE = (
    '#!/bin/sh\nmkdir -p workspace/idea1\necho out > workspace/idea1/out.txt\n'
    'cat workspace/NOTES.md > workspace/idea1/seen.txt\nrm workspace/NOTES.md\necho mine > workspace/NOTES.md\n'
    'echo line >> solve.txt\ngit commit -qam "add a line"\n'
)


@pytest.mark.usefixtures('daemon_api')
def test_what_an_implementer_writes_in_its_workspace_is_merged_into_the_campaigns_after_its_run(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The implementer starts from a snapshot, and its writes land when its report is applied, not while it works."""
    live = tmp_path / 'projects' / str(campaign.id) / 'workspace'
    live.mkdir(parents=True)
    (live / 'NOTES.md').write_text('the notes\n')
    write_toml(Path(campaign.path), HARNESS_WRITES_THE_WORKSPACE + END_RUN)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (live / 'idea1' / 'out.txt').read_text() == 'out\n'
    assert (live / 'idea1' / 'seen.txt').read_text() == 'the notes\n', 'it started from the workspace'
    assert (live / 'NOTES.md').read_text() == 'mine\n'
    assert run_row(db, run_id).status is RunStatus.SUCCEEDED
    [c] = candidates(db, run_id)
    assert c.summary == 'add a line', 'what it wrote in the workspace is not in its commit'


def test_every_commit_is_scored_and_descends_from_the_one_before(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    first, second = candidates(db, run_id)
    assert (first.summary, first.metrics['score'], first.parent_id) == ('first', 2.0, 1)
    assert (second.summary, second.metrics['score'], second.parent_id) == ('second', 3.0, first.id)
    assert (first.best, second.best) == (False, True)
    assert (first.told, second.told) == snapshot(
        (
            'score 2 over 1 eval: the best of the score track now, rank 1 of 2; vs parent +1',
            'score 3 over 1 eval: the best of the score track now, rank 1 of 3; vs parent +1',
        )
    )


def test_every_commit_of_a_run_is_a_candidate_before_the_first_is_measured(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit became a candidate only when the evaluations of the commits before it were
    done, 33 minutes after its run, so it looked lost. Each commit is a pending candidate when its run closes."""
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    measure = ex._measure
    seen: list[list[tuple[str | None, int, int | None]]] = []

    def watched(
        run: RunId | None,
        campaign_id: CampaignId,
        cfg: CampaignConfig,
        sha: Revision,
        candidate_id: CandidateId,
        evaluator_id: EvaluatorId,
        eval_dir: Path,
        *,
        repeats: int = 1,
        gate: CandidateId | None = None,
        split: Split = Split.VISIBLE,
    ) -> Measuring:
        with db.session() as s:
            rows = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
            seen.append([(c.summary, len(c.measurements), c.parent_id) for c in rows])
        return measure(
            run, campaign_id, cfg, sha, candidate_id, evaluator_id, eval_dir, repeats=repeats, gate=gate, split=split
        )

    monkeypatch.setattr(ex, '_measure', watched)
    execute(ex, db, run_id)
    first, second = candidates(db, run_id)
    assert seen[0] == [('first', 0, 1), ('second', 0, first.id)], 'all at once, in order'
    assert (first.metrics['score'], second.metrics['score'], second.best) == (2.0, 3.0, True)


@pytest.mark.parametrize('moved_to', [None, IdeaStatus.DROPPED, IdeaStatus.DROPPED])
def test_a_run_counts_toward_max_runs_from_its_close_to_its_score(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    moved_to: IdeaStatus | None,
) -> None:
    """A run keeps its place under ``max_runs`` from its close until its commits are scored, before its candidates
    exist, and also when a human parks or rejects its idea meanwhile (``piloop watch`` allows it)."""
    ex = controller(db, runtime, tmp_path)
    close = ex._close
    counted: list[tuple[int, bool]] = []

    def close_and_count(span: Span, run_id: RunId, end: RunEnd, error: str | None) -> None:
        close(span, run_id, end, error)
        with db.session() as s:
            if moved_to is not None:
                idea_of(s.get_one(Run, run_id)).status = moved_to
                s.commit()
            campaign_row = s.get_one(Campaign, campaign.id)
            counted.append(
                (runs_stop.runs(s, campaign_row), bool(s.scalar(select(Run.id).where(runs_stop.unsettled()))))
            )

    mocker.patch.object(ex, '_close', side_effect=close_and_count)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    assert counted == [(1, True)], 'counted and unsettled while it is scored'
    with db.session() as s:
        assert s.scalar(select(Run.id).where(runs_stop.unsettled())) is None, f'run#{run_id} settled'


@pytest.mark.usefixtures('daemon_api')
def test_a_worse_commit_is_scored_below_the_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_WORSE + END_RUN)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [e] = candidates(db, run_id)
    assert (e.best, e.metrics['score']) == (False, 0.0)
    assert e.told == snapshot('score 0 over 1 eval: rank 2 of 2; vs parent -1, vs best candidate#1 -1')
    assert run_row(db, run_id).status is RunStatus.SUCCEEDED


def test_a_best_rule_from_a_plugin_replaces_the_built_in_one_through_piloop_toml(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The example plugin ``examples/plugins/min-gain``, installed, is selected by ``[plugins.best_rule]``.

    The daemon scores the run's commit with it: ``baseline`` would let a gain of 1 be best, ``min_gain = 2`` does not.
    """
    example(tmp_path / 'site', 'min-gain')
    monkeypatch.syspath_prepend(tmp_path / 'site')
    host.close()
    try:
        for table, error in [
            ({'provider': 'strict'}, "plugins.best_rule: provider = 'strict': unknown best rule 'strict'"),
            ({'provider': 'min_gain', 'min_gain': 'x'}, "min_gain = 'x': Input should be a valid number"),
            (
                {'provider': 'baseline', 'min_gain': 2},
                'plugins.best_rule.min_gain = 2: Extra inputs are not permitted',
            ),
        ]:
            with pytest.raises(ConfigError, match=re.escape(error)):
                load_campaign_config(Path(campaign.path), plugins={'best_rule': table})
        with db.session() as s:
            cfg = load_campaign_config(
                Path(campaign.path), plugins={'best_rule': {'provider': 'min_gain', 'min_gain': 2}}
            )
            s.get_one(Campaign, campaign.id).config = cfg
            s.commit()
        run_id = make_run(db, campaign)
        execute(controller(db, runtime, tmp_path), db, run_id)
        [e] = candidates(db, run_id)
        assert (e.best, e.metrics['score']) == (False, 2.0)
        assert e.told == snapshot(
            'score 2 over 1 eval: it may not be best: its gain 1 is below min_gain 2; vs parent +1, vs best candidate#1 +1'
        )
        assert BEST_RULES.sources()['min_gain'].release == 'piloop-min-gain 0.1.0'
        stored = f'in the config of demo that the last `piloop init` stored from {campaign.path}/piloop.toml'
        selected = f'demo: best rule min_gain, from piloop-min-gain 0.1.0 ([plugins.best_rule] provider, {stored})'
        assert selected in selections(Settings(home=tmp_path))
    finally:
        host.close()


def test_work_a_stopped_implementer_left_uncommitted_is_recovered_and_its_idea_is_open_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A failed implementer's uncommitted tree was scored, and could become the best, never declared ready.

    It is now ``recovered``: committed, pinned, never measured, never in the pool. The run's claim ends: its idea is
    open for the next worker, which starts from the best.
    """
    write_toml(Path(campaign.path), HARNESS_FORGETS_COMMIT)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (run_row(db, run_id).end, run_row(db, run_id).error) == (RunEnd.GAVE_UP, None)
    [(summary, status, told, measurements, _)] = scored(db, run_id)
    assert (summary, status, told, measurements) == (UNCOMMITTED, Origin.RECOVERED, None, 0)
    with db.session() as s:
        recovered = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).one()
        assert s.get_one(Idea, claimed_id(s.get_one(Run, run_id))).status is IdeaStatus.OPEN
        assert [c.body for c in s.scalars(select(Comment).where(Comment.target_kind == IDEA))] == [
            f'open: run#{run_id} gave the idea back; it ended gave_up'
        ], 'the note says why, and nothing is requeued'
        best = best_of(s, s.get_one(Campaign, campaign.id))
        assert best is not None and best.id == 1, 'the best did not move: the baseline is candidate#1'
        recovered_sha = recovered.revision
    repo = Repo(campaign.path)
    assert 'data' not in repo.git.show('--stat', '--format=', recovered_sha), 'linked paths are never committed'
    assert repo.git.log('-1', '--format=%(trailers:key=Piloop-Recovered,valueonly)', recovered_sha).strip() == (
        f'run#{run_id}'
    )


@pytest.mark.usefixtures('daemon_api')
def test_dirt_after_a_commit_is_recovered_on_its_own_and_the_commit_is_scored_as_made(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_COMMITS_AND_LEAVES_DIRT + END_RUN)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert [(c[0], c[1], c[3]) for c in scored(db, run_id)] == [
        ('the idea', 'best', 1),
        (UNCOMMITTED, Origin.RECOVERED, 0),
    ], 'never amended: the implementer declared its commit, not the dirt after it'
    [e] = [c for c in candidates_measured(db, run_id)]
    assert e.metrics['score'] == 2.0
    with db.session() as s:
        assert s.get_one(Idea, claimed_id(s.get_one(Run, run_id))).status is IdeaStatus.DONE


HARNESS_LEAVES_DIRT_WHILE_A_JOB_WRITES_THE_WORKSPACE = (
    '#!/bin/sh\ngit config filter.job.clean "tee -a %f"\nmkdir -p "$(git rev-parse --git-common-dir)/info"\n'
    'echo "workspace/** filter=job" > "$(git rev-parse --git-common-dir)/info/attributes"\n'
    'echo log > workspace/run.log\ngit add -f workspace/run.log\ngit commit -qm "keep the log"\n'
    'echo line >> solve.txt\n'
)
"""Leaves an edit of ``solve.txt`` unsaved. The filter is a job that writes the log whenever git reads it."""


def test_dirt_is_recovered_while_a_job_still_writes_the_workspace(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The save never reads the workspace, so the job's writes there cannot make git refuse it."""
    (tmp_path / 'projects' / str(campaign.id) / 'workspace').mkdir(parents=True)
    write_toml(Path(campaign.path), HARNESS_LEAVES_DIRT_WHILE_A_JOB_WRITES_THE_WORKSPACE)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert [c[0] for c in scored(db, run_id)] == ['keep the log', UNCOMMITTED]
    with db.session() as s:
        recovered = s.scalars(select(Candidate.revision).where(Candidate.summary == UNCOMMITTED)).one()
    repo = Repo(campaign.path)
    assert repo.git.show('--name-only', '--format=', recovered) == 'solve.txt'
    assert repo.git.show(f'{recovered}:workspace/run.log') == 'log', 'as the implementer committed it'


def test_a_clean_exit_with_no_changes_gives_the_run_up_and_opens_its_idea_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(Path(campaign.path), HARNESS_NOOP)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path, agents=WORKED), db, run_id)
    run = run_row(db, run_id)
    assert (run.end, run.error) == (RunEnd.GAVE_UP, None)
    with db.session() as s:
        assert s.get_one(Idea, run.idea_id).status is IdeaStatus.OPEN, 'the claim ended with the run'


def test_timeout(db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path) -> None:
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=0.5)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.TIMED_OUT, 'agent timeout after 0.5s')
    with db.session() as s:
        [agent_session] = s.scalars(select(AgentSession)).all()
        assert agent_session.state is AgentSessionState.KILLED


def test_a_run_whose_session_the_stop_cut_short_ends_as_cut_by_the_stop_and_is_no_attempt(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Its session ends at the ``max_hours`` stop, before its role timeout: the stop ended it, not its idea's attempt."""
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=3600)
    set_config(db, campaign, plugins={'stop_rule': {'max_hours': 1}})
    first = make_run(db, campaign)
    with db.session() as s:
        run = s.get_one(Run, first)
        run.started_at = utcnow() - timedelta(seconds=3600 - 3)  # max_hours counts from it: 3 s left
        close_run(s, run, RunEnd.FINISHED, None)
        s.commit()
    run_id = make_run(db, campaign, title='late')
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.end, run.error) == (RunEnd.CAMPAIGN_STOPPED, None)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert runs_stop.runs_without_best(s, p) == 1, 'the first run only: the cut one is no attempt'


def test_a_role_timeout_that_init_raises_applies_to_the_next_run(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run that is open keeps its timeout: ``piloop run extend`` gives it more."""
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=0.5)
    reload_campaign(db, campaign)
    ex = controller(db, runtime, tmp_path)
    first = make_run(db, campaign)
    execute(ex, db, first)
    write_toml(Path(campaign.path), HARNESS_SLEEPS, implementer_timeout=0.7)
    with db.session() as s:
        sync(s, s.get_one(Campaign, campaign.id))  # what ``piloop init`` does
        s.commit()
    second = make_run(db, campaign, title='after the init')
    execute(ex, db, second)
    assert [run_row(db, r).error for r in (first, second)] == ['agent timeout after 0.5s', 'agent timeout after 0.7s']


@pytest.mark.usefixtures('submitting')
def test_an_evaluate_failure_is_a_crash(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:

    cfg = load_campaign_config(
        Path(campaign.path),
        evaluate=['sh', '-c', f'test $(wc -l < solve.txt) -eq 1 && {reports(score="1", size="1")} || exit 9'],
    )
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [e] = candidates(db, run_id)
    assert (e.best, e.told, e.metrics) == (False, 'crash: no evaluation succeeded', {})
    assert e.error is not None and e.error.startswith('evaluate exit 9')
    assert run_row(db, run_id).status is RunStatus.SUCCEEDED, 'the implementer did its job; the candidate crashed'


def test_a_crash_from_a_fault_of_the_host_is_measured_again_on_request_and_scored(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A crash that the host caused (a full disk) says nothing about the code, and its tree cannot be committed again.

    ``piloop candidate remeasure`` asks the daemon to measure the candidate again with the current evaluator. Once the
    fault is gone, the candidate is scored into the pool as its run would have scored it.
    """
    fault = tmp_path / 'disk-full'
    measure = f'test -e {fault} && exit 28; ' + reports(score='$(wc -l < solve.txt)', size='$(wc -c < solve.txt)')
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=['sh', '-c', measure])
    reload_campaign(db, campaign)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)  # the new evaluator measures the baseline first, as the daemon does
    run_id = make_run(db, campaign)
    fault.touch()
    execute(ex, db, run_id)
    [crashed] = candidates(db, run_id)
    assert (crashed.best, crashed.told) == (False, 'crash: no evaluation succeeded')
    with db.session() as s:
        request_remeasure(s, s.get_one(Candidate, crashed.id), System.ORCHESTRATOR, repeats=1)
        s.commit()
    ex.remeasure(crashed.id)
    with db.session() as s:
        c = s.get_one(Candidate, crashed.id)
        assert [m.error is None for m in c.measurements] == [False, False], 'the fault is still there: a crash again'
        assert told_of(s, crashed.id) == 'crash: no evaluation succeeded'
    fault.unlink()
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert requested(s, p) == [], 'nobody asked'
        request_remeasure(s, s.get_one(Candidate, crashed.id), System.ORCHESTRATOR)
        s.commit()
        assert [c.id for c, _ in requested(s, p)] == [crashed.id]
    ex.remeasure(crashed.id)
    with db.session() as s:
        c = s.get_one(Candidate, crashed.id)
        assert [m.error is None for m in c.measurements] == [False, False, True, True, True]
        p = s.get_one(Campaign, campaign.id)
        assert requested(s, p) == [], 'measured since the request'
        assert best_of(s, p) == c
        assert told_of(s, crashed.id) == snapshot(
            'score 2 over 3 evals: the best of the score track now, rank 1 of 2; vs parent +1'
        )


def test_a_crash_on_the_evaluate_timeout_is_measured_again_on_request_once_the_timeout_is_raised(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A load that slows the evaluator past ``evaluate_timeout`` crashes good code, and its tree cannot be committed
    again. The timeout is no part of the evaluator, so once it is raised, ``piloop candidate remeasure`` measures the
    candidate again under the same evaluator, with the new timeout, and scores it again."""
    measure = f'test $(wc -l < solve.txt) -gt 1 && sleep 1; {reports(score="$(wc -l < solve.txt)", size="1")}'
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=['sh', '-c', measure])
    toml = Path(campaign.path) / 'piloop.toml'
    toml.write_text(toml.read_text().replace('evaluate_timeout = 30', 'evaluate_timeout = 0.2'))
    reload_campaign(db, campaign)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    [crashed] = candidates(db, run_id)
    assert (crashed.told, crashed.error) == ('crash: no evaluation succeeded', 'evaluate timeout after 0.2s')
    with db.session() as s:
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id
    toml.write_text(toml.read_text().replace('evaluate_timeout = 0.2', 'evaluate_timeout = 30'))
    reload_campaign(db, campaign)
    with db.session() as s:
        request_remeasure(s, s.get_one(Candidate, crashed.id), System.ORCHESTRATOR, repeats=1)
        s.commit()
    ex.remeasure(crashed.id)
    with db.session() as s:
        c = s.get_one(Candidate, crashed.id)
        assert [m.evaluator_id == evaluator_id for m in c.measurements] == [True, True], 'the same evaluator'
        assert told_of(s, c.id) == snapshot(
            'score 2 over 1 eval: the best of the score track now, rank 1 of 2; vs parent +1'
        )
        assert best_of(s, s.get_one(Campaign, campaign.id)) == c


def test_a_remeasure_that_a_stop_cancels_records_and_scores_nothing(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    ex = controller(db, runtime, tmp_path)
    ex.launcher.cancel()
    ex.remeasure(baseline(db, campaign.id).id)
    with db.session() as s:
        c = s.get_one(Candidate, baseline(db, campaign.id).id)
        assert (len(c.measurements), in_order(s, CandidateScored)) == (1, []), 'the stop owns the rows'


def test_brief_carries_history_and_comments(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_PROMPT_TO_FILE)
    reload_campaign(db, campaign)
    first = make_run(db, campaign, title='first idea')
    execute(controller(db, runtime, tmp_path), db, first)  # produces an uncommitted prompt.txt → scored as a commit
    second = make_run(db, campaign, title='second idea')
    with db.session() as s:
        idea_id = claimed_id(s.get_one(Run, second))
        s.add(Comment(target=TargetRef(IDEA, idea_id), author='human:t', body='try harder'))
        s.add(Comment(target=TargetRef(RUN, second), author='pi', kind='decision', body='and faster'))
        s.add(Comment(target=TargetRef(RUN, first), author='pi', body='about another idea'))
        s.commit()
    execute(controller(db, runtime, tmp_path), db, second)
    brief = brief_of(db, second)
    order = [
        '# Implementer',
        '# Demo',
        '# Context',
        '## Comments on this idea',
        '## History: ideas',
        '## History: new bests',
    ]
    assert [brief.index(s) for s in order] == sorted(brief.index(s) for s in order), 'the fixed brief order'
    assert '## Your idea' not in brief and 'candidate#2' in brief, 'a fresh run reads the board, not one idea'
    assert '`piloop idea claim <id>`' in brief and "`piloop idea propose '<title>' --hypothesis" in brief
    assert (
        '## Comments on this idea and its runs\n\n- human:t (note): try harder\n- pi (decision on run#2): and faster\n'
        in (brief)
    ), "the idea's comments and those on any of its runs, in order, each run comment saying which run"
    assert 'about another idea' not in brief
    assert '| 1 ' in brief and 'first idea' in brief, 'the same history block the research agent reads'
    assert 'Tried so far' not in brief
    assert '"$PILOOP_BIN" sql' in brief and "'implementer#2', 'note'" in brief
    assert (
        '- Your session ends at `$PILOOP_DEADLINE` (Unix time), 2 minutes after it starts. You are told to wrap up'
        ' 0.5 minutes before: start no command that can run past it.\n' in brief
    ), 'the role timeout of 120s'
    assert '`piloop escalate idea#<your idea> --priority 1`' in brief, 'a request is an escalation, never a note'
    assert re.search(r'`"\$PILOOP_BIN" schema <guide>` prints another guide: .*\blong_jobs\b', brief), (
        'how to wait for a long job is a guide away'
    )
    assert 'kills every process that you started' in LongJobs.TEXT.read_text(), 'nothing outlives the harness'
    assert 'When you are done, stop: your run ends with what you submitted.' in brief, 'how its run ends'
    assert '`piloop run end submitted`' not in brief, 'its exit ends its run'
    assert '- A hunch for another run goes in a note' in brief and 'idea seed demo' not in brief


def test_the_comments_on_an_idea_are_capped_by_their_section(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A long thread of comments cannot crowd out the brief: the latest ones, each cut, then a query for the rest."""
    run_id = make_run(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.config = load_campaign_config(
            Path(campaign.path),
            plugins={
                'section': {'idea_comments': {'latest': 2, 'chars': 12}},
                'role': {'implementer': {'sections': ['idea_comments']}},
            },
        )
        idea_id = claimed_id(s.get_one(Run, run_id))
        for body in ('first and oldest', 'second', 'third, which runs on and on'):
            s.add(Comment(target=TargetRef(IDEA, idea_id), author='human:t', body=body))
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    brief = brief_of(db, run_id)
    assert (
        '## Comments on this idea and its runs\n\n'
        '- 1 older comments: `SELECT body FROM comments` reads them.\n'
        '- human:t (note): second\n'
        '- human:t (note): third, which… (comment#3 has the rest)\n'
    ) in brief
    assert 'first and oldest' not in brief


def test_an_implementer_brief_names_its_checkout_as_the_place_to_work_and_never_the_campaign_repos_path(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """Given only the campaign repo's path, an implementer edited and committed there: never scored, and main moved.

    The brief names no path: the worker makes its checkout, on whichever machine runs the job, and a path to the
    campaign repo is a place to edit. Only the vcs provider's parts of it say git or commit."""

    def marked(self: Git, part: Part) -> str:
        return f'<{part}>'

    mocker.patch.object(Git, 'prompt', marked)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    brief = brief_of(db, run_id)
    assert 'You are a piloop implementer, in <checkout> of the campaign' in brief
    assert "- Work in your checkout and in your idea's directory in `workspace/`, never in the campaign repo" in brief
    assert campaign.path not in brief
    assert git_words(brief) == []


@pytest.mark.parametrize('sections', [['context', 'lessons', 'history'], ['context', 'history']])
def test_an_implementer_brief_ends_with_its_roles_sections(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, sections: list[str]
) -> None:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        p.repo = 'github.com/o/r'
        p.config = load_campaign_config(Path(campaign.path), plugins={'role': {'implementer': {'sections': sections}}})
        s.add(
            Lesson(
                campaign_id=campaign.id,
                family='voting',
                status=LessonStatus.DEAD,
                effect=0,
                metric='score',
                body='idea#1 on evaluator#1',
                created_by='orchestrator#9',
            )
        )
        s.commit()
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    brief = brief_of(db, run_id)
    line = '- [demo] voting | dead | +0 score (lesson#1)'
    if 'lessons' in sections:
        assert brief.index('# Context') < brief.index(line) < brief.index('## History'), (
            'after the context, in the order of the list'
        )
    else:
        assert line not in brief and 'What past campaigns' not in brief, 'a list without the lessons'
        assert '## History' in brief


def test_pre_written_brief_is_used_verbatim(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    run_id = make_run(db, campaign)
    with db.session() as s:
        s.get_one(Run, run_id).brief_cid = home_cas(db).put(b'custom brief')
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (tmp_path / 'runs' / str(run_id) / 'brief.md').read_text() == 'custom brief'


def test_exception_in_run_is_recorded(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    run_id = make_run(db, campaign)
    with db.session() as s:
        s.get_one(Run, run_id).base_candidate_id = CandidateId(999)
        s.commit()
    execute(controller(db, runtime, tmp_path, tracer), db, run_id)
    run = run_row(db, run_id)
    assert run.status is RunStatus.FAILED and run.error is not None and run.error.startswith('NoResultFound')
    with db.session() as s:
        assert idea_of(s.get_one(Run, run_id)).status is IdeaStatus.OPEN, 'a fault before the launch: the claim ends'
    [span] = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR and [e.name for e in span.events] == ['exception']


def test_a_new_evaluator_measures_the_baseline_and_the_old_best_then_what_someone_asked_for_then_new_references(
    db: Database, campaign: Campaign
) -> None:
    """After an evaluator change, the daemon measures the baseline, the best under the old evaluator, the asks and the
    new references, in that order.

    Every other candidate keeps its old scores until someone asks: measuring the whole history again held every
    evaluation for hours after each change of the evaluator.
    """
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        old = proj.evaluator_id
        assert old is not None
        rows: dict[str, Candidate] = {}
        for tree, origin, score in (('a', Origin.RUN, 9.0), ('b', Origin.RUN, 1.0), ('ref', Origin.REFERENCE, 5.0)):
            rows[tree] = c = Candidate(
                campaign_id=campaign.id, tree_sha=tree, revision=tree, origin=origin, parent_id=1
            )
            s.add(c)
            s.flush()
            s.add(Measurement(candidate_id=c.id, evaluator_id=old, metrics={'score': score}))
        fresh = Candidate(campaign_id=campaign.id, tree_sha='fresh', revision='fresh', origin=Origin.REFERENCE)
        s.add(fresh)
        request_remeasure(s, rows['b'], System.ORCHESTRATOR)
        s.flush()
        assert [c.tree_sha for c in due(s, proj)] == ['b', 'fresh'], 'the ask, and a reference that nothing measured'
        proj.evaluator_id = new = s.scalar(
            insert(Evaluator).values(campaign_id=campaign.id, key='next', definition={}).returning(Evaluator.id)
        )
        s.flush()
        first = s.get_one(Candidate, 1)
        assert [c.tree_sha for c in due(s, proj)] == ['a', first.tree_sha, 'b', 'fresh'], (
            'the best under the old evaluator; the baseline; the ask; the new reference. Not ref, which scored 5'
        )
        for c in (first, rows['a']):
            s.add(Measurement(candidate_id=c.id, evaluator_id=new, metrics={'score': 1.0}))
        s.flush()
        s.expire_all()
        assert [c.tree_sha for c in due(s, proj)] == ['b', 'fresh'], 'measured: no longer due'


@pytest.mark.usefixtures('submitting')
def test_transcript_failure_is_only_an_event(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    mocker: MockerFixture,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    mocker.patch.object(runtime, 'read', side_effect=OSError('pane gone'))
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path, tracer), db, run_id)
    assert run_row(db, run_id).status is RunStatus.SUCCEEDED
    assert events(spans, 'agent_session.transcript_lost') == [{'error': 'pane gone'}]


def test_a_rejection_during_the_run_is_not_undone(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    run_id = make_run(db, campaign)
    with db.session() as s:
        s.get_one(Idea, claimed_id(s.get_one(Run, run_id))).status = IdeaStatus.DROPPED
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        assert s.get_one(Idea, 1).status is IdeaStatus.DROPPED


def test_implementer_env_points_at_the_daemon_home(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The implementer sees the daemon's home, its PATH after its own piloop, and no harness session of the daemon.

    A daemon started from a Claude Code session inherits its variables, and a Claude agent that has them acts as a
    child of that session, which saves no transcript. They are cleared for every agent, whatever its harness
    definition: ``[plugins.harness.claude]`` replaces the built-in one, and a custom harness can run the same CLI. This one is
    the ``script`` harness of the fixture, with a session variable of its own.
    """
    cleared = {*cleared_env(), 'SCRIPT_SESSION'}
    for name in cleared:
        monkeypatch.setenv(name, '1')
    write_toml(Path(campaign.path), '#!/bin/sh\nenv > env.txt\ngit add env.txt\ngit commit -qm env\n')
    with (Path(campaign.path) / 'piloop.toml').open('a') as toml:  # [plugins.harness.script] is the last table
        toml.write('session_env = ["SCRIPT_SESSION"]\n')
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, ScriptRuntime(), tmp_path), db, run_id)
    [e] = candidates(db, run_id)
    lines = Repo(campaign.path).git.show(f'{e.revision}:env.txt').splitlines()
    env = dict(line.split('=', 1) for line in lines if '=' in line)
    assert (env['PILOOP_HOME'], env['PATH']) == (str(tmp_path), f'{tmp_path / "bin"}{os.pathsep}{os.environ["PATH"]}')
    assert {name: env.get(name) for name in cleared} == dict.fromkeys(cleared, '')


def test_lineage_follows_the_actual_predecessor(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(
        Path(campaign.path),
        '#!/bin/sh\n: > solve.txt\ngit commit -qam worse\necho a >> solve.txt\necho b >> solve.txt\ngit commit -qam better\n',
    )
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    worse, better = candidates(db, run_id)
    assert (worse.best, better.best) == (False, True)
    assert better.parent_id == worse.id, 'built on the worse commit'


def test_an_implementer_that_never_got_going_is_relaunched_on_its_run_then_the_run_fails(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """An implementer that dies at startup is not an attempt: the same run is queued again for a new implementer.

    The dead implementer's transcript is kept aside. A run whose every launch died fails, but it is still no attempt: its
    claim ends, and its idea is open for another worker.
    """
    run_id = make_run(db, campaign)
    write_toml(Path(campaign.path), '#!/bin/sh\necho Welcome\n')
    reload_campaign(db, campaign)
    ex = controller(db, runtime, tmp_path)
    for launches in (1, 2):
        execute(ex, db, run_id)
        with db.session() as s:
            run = s.get_one(Run, run_id)
            assert (run.status, run.finished_at, idea_of(run).status) == (RunStatus.QUEUED, None, IdeaStatus.CLAIMED)
            rows = s.scalars(select(AgentSession).where(working_on(run_id)).order_by(AgentSession.id)).all()
            assert [w.state for w in rows] == [
                *[AgentSessionState.NEVER_WORKED] * launches,
                AgentSessionState.REQUESTED,
            ]
            assert (rows[-1].harness, rows[-1].model) == ('script', 'test-model')
            assert rows[-1].reason == f'run#{run_id} relaunched: implementer#{rows[-2].id} failed to start', 'its role'
            assert runs_stop.runs(s, run.campaign) == 1
        assert (tmp_path / 'runs' / str(run_id) / f'transcript-implementer{rows[-2].id}.txt').read_text() == 'Welcome'
    execute(ex, db, run_id)
    with db.session() as s:
        relaunched = in_order(s, RunRelaunched)
        assert [p.launches for _, p in relaunched] == [1, 2]
        idea = s.get_one(Idea, claimed_id(s.get_one(Run, run_id)))
        assert idea.status is IdeaStatus.OPEN, 'no agent of its run started to work: the claim ends with the run'
        assert [c.body for c in s.scalars(select(Comment).where(Comment.target_id == idea.id))] == [
            f'open: run#{run_id} gave the idea back; it ended not_started'
        ]
    run = run_row(db, run_id)
    assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.NOT_STARTED)
    with db.session() as s:
        assert [a.error for _, a in in_order(s, StartupAnomaly)] == [
            f'{never_worked(AgentSettings().startup_seconds)}: Welcome'
        ] * 3, 'each says why it failed, with its last output'

    mocker.patch.object(runtime, 'start', side_effect=RuntimeError('no server'))
    retried = make_run(db, campaign, title='retried')
    for _ in range(3):
        execute(ex, db, retried)
    with db.session() as s:
        assert s.get_one(Idea, claimed_id(run_row(db, retried))).status is IdeaStatus.OPEN


@pytest.mark.parametrize('held', ['lead', 'paused', 'stopped'])
def test_a_run_whose_implementer_never_worked_ends_at_once_when_its_campaign_would_not_launch_it_now(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, held: str
) -> None:
    """#736: a relaunch that cannot start now would wait with its run open, and keep its slot.

    Within the wrap-up lead, its implementer would be told to wrap up as it starts. While the campaign is paused, it
    would wait for the resume, and once it stopped, nothing launches it. The run ends ``not_started`` instead: no attempt, and its idea goes back to the board.
    """
    write_toml(Path(campaign.path), '#!/bin/sh\necho Welcome\n', implementer_timeout=3600)
    reload_campaign(db, campaign)
    if held == 'lead':
        set_config(db, campaign, plugins={'stop_rule': {'max_hours': 1}})
        first = make_run(db, campaign, title='first')
        with db.session() as s:
            run = s.get_one(Run, first)
            run.started_at = utcnow() - timedelta(seconds=3600 - 60)  # max_hours counts from it: 60 s left
            close_run(s, run, RunEnd.FINISHED, None)
            s.commit()
    run_id = make_run(db, campaign)
    with db.session() as s:
        if held == 'paused':
            ops.pause(s, campaign.id, evals=False, reason='a person reads the results')
        if held == 'stopped':  # the stop's wind-down ends a run in flight: this one died before it
            row = s.get_one(Campaign, campaign.id)
            row.status, row.stopped_reason = CampaignStatus.STOPPED, 'by hand'
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        rows = s.scalars(select(AgentSession).where(working_on(run_id))).all()
        assert (run.status, run.end, [w.state for w in rows]) == (
            RunStatus.FAILED,
            RunEnd.NOT_STARTED,
            [AgentSessionState.NEVER_WORKED],
        )
        assert in_order(s, RunRelaunched) == []
        assert idea_of(run).status is IdeaStatus.OPEN


def test_a_run_that_a_stop_closed_after_the_pass_took_its_implementer_never_starts_and_closes_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The stop's pass drained the queued run while its implementer was ``starting``, before the run's thread ran.

    The thread used to set ``running`` on the closed run. The CHECK refused that, and the run closed a second time,
    ``not_started`` over ``campaign_stopped``. Now the thread starts nothing: the run's first end stands.
    """
    run_id = make_run(db, campaign)
    with db.session() as s:  # one pass takes the implementer
        implementer = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        implementer.state = AgentSessionState.STARTING
        s.commit()
        agent_session_id = implementer.id
    with db.session() as s:  # the next pass drains the run, which is still queued
        run = s.get_one(Run, run_id)
        release(s, close_run(s, run, RunEnd.CAMPAIGN_STOPPED, None), RunEnd.CAMPAIGN_STOPPED, None)
        s.commit()
    controller(db, runtime, tmp_path).execute(run_id, agent_session_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, run.error) == (RunStatus.FAILED, RunEnd.CAMPAIGN_STOPPED, None)
        assert [f.end for _, f in in_order(s, RunFinished)] == [RunEnd.CAMPAIGN_STOPPED]
        assert in_order(s, RunStarted) == []
        implementer = s.get_one(AgentSession, agent_session_id)
        assert (implementer.state, implementer.error) == (
            AgentSessionState.KILLED,
            f'run#{run_id} is failed: it cannot become running',
        )
        with pytest.raises(Refused, match='cannot become failed'):
            close_run(s, run, RunEnd.FAILED, 'again')


def test_a_run_closed_by_hand_while_its_implementer_starts_keeps_the_end_written_by_hand(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """``piloop sql`` is the escape hatch: a person closed the run. Its thread neither queues it again nor closes it."""
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)

    def closed_by_hand(*_: object, **__: object) -> NotStarted:
        with db.session() as s:
            ended = {'status': RunStatus.FAILED, 'end': RunEnd.CANCELLED, 'finished_at': utcnow()}
            s.execute(update(Run).where(Run.id == run_id).values(ended))
            s.commit()
        return NotStarted('it failed to start')

    mocker.patch.object(ex.launcher, 'launch', side_effect=closed_by_hand)
    execute(ex, db, run_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.CANCELLED)
        assert in_order(s, RunFinished) == [] and in_order(s, RunRelaunched) == []
        assert [w.state for w in s.scalars(select(AgentSession).where(working_on(run_id)))] == [
            AgentSessionState.KILLED
        ]


def test_a_worktree_that_cannot_be_made_is_a_failed_start_that_frees_the_slot(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A run whose worktree cannot be made closes its implementer as a failed start, so the implementer holds no slot."""
    full = GitCommandError(['git', 'worktree', 'add'], 128, b'fatal: Could not write new index file')
    mocker.patch('piloop.plugins.vcs.git.commands.add_worktree', side_effect=full)
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    for _ in range(3):
        execute(ex, db, run_id)
    with db.session() as s:
        rows = s.scalars(select(AgentSession).where(working_on(run_id)).order_by(AgentSession.id)).all()
        assert [w.state for w in rows] == [AgentSessionState.NEVER_WORKED] * 3
        assert all('Could not write new index file' in (w.error or '') for w in rows)
        assert s.scalars(select(Event.target_id).where(has(StartupAnomaly))).all() == [w.id for w in rows]
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, idea_of(run).status) == (RunStatus.FAILED, RunEnd.NOT_STARTED, IdeaStatus.OPEN)


def test_a_run_that_fails_before_its_agent_starts_closes_its_implementer(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A fault of the daemon before the launch fails the run and closes its implementer, which then holds no slot.

    The idea was not tried: the run is no attempt, and its claim ends: the idea is open again, not done.
    """
    mocker.patch('piloop.plugins.vcs.git.commands.open_repo', side_effect=RuntimeError('repo gone'))
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        [w] = s.scalars(select(AgentSession).where(working_on(run_id))).all()
        assert (w.state, w.error, w.ended_at is not None) == (AgentSessionState.KILLED, 'RuntimeError: repo gone', True)
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, run.error, idea_of(run).status) == (
            RunStatus.FAILED,
            RunEnd.NOT_STARTED,
            'RuntimeError: repo gone',
            IdeaStatus.OPEN,
        )


def test_an_implementer_that_spends_tokens_but_ends_at_once_with_nothing_did_not_start_to_work(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implementer whose tools fail at launch still reads its brief and says it is blocked, which costs tokens.

    A run's work is a commit or a comment, so an implementer that ends within the startup window without either did not
    start to work, whatever it spent. Its run is queued again, not counted as an attempt.
    """
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    run_id = make_run(db, campaign)
    folder = home / '.pi' / 'agent' / 'sessions' / dash_separators_wrapped(tmp_path / 'runs' / str(run_id) / 'worktree')
    blocked = f'#!/bin/sh\nmkdir -p {folder}\ncp {SESSIONS / "pi.jsonl"} "{folder}/2026-09-09T05-48-14-300Z_$2.jsonl"\n'
    write_toml(Path(campaign.path), blocked)
    script = load_campaign_config(Path(campaign.path)).harness['script'].model_dump(mode='json')
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    set_config(db, campaign, plugins={'harness': {'script': {**script, **counted}}})
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        dead, relaunched = s.scalars(select(AgentSession).where(working_on(run_id)).order_by(AgentSession.id)).all()
        assert (dead.state, dead.usage.total if dead.usage else None, relaunched.state) == (
            AgentSessionState.NEVER_WORKED,
            16478,
            AgentSessionState.REQUESTED,
        )
        assert s.scalars(select(Event.target_id).where(has(StartupAnomaly))).all() == [dead.id]
        run = s.get_one(Run, run_id)
        assert (run.status, run.error, idea_of(run).status) == (RunStatus.QUEUED, None, IdeaStatus.CLAIMED), (
            'the same run'
        )


@pytest.mark.parametrize('on', ['run', 'agent'])
def test_a_cancel_made_while_the_implementer_starts_survives_a_failed_start(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture, on: str
) -> None:
    """The daemon leaves a starting agent alone. When its start then failed, the run was queued again with a new
    implementer, which the cancel did not name, and the cancelled run ran after all."""
    run_id = make_run(db, campaign)

    def cancel_then_fail(spec: object) -> Handle:
        with db.session() as s:
            run = s.get_one(Run, run_id)
            [agent_session] = s.scalars(select(AgentSession).where(working_on(run_id))).all()
            if on == 'run':
                stop.request_cancel(s, run, System.HUMAN, ending=Ending.NOW)
            else:
                stop.request_cancel(s, agent_session, System.HUMAN, ending=Ending.NOW)
            s.commit()
        raise RuntimeError('no pickup')

    mocker.patch.object(runtime, 'start', side_effect=cancel_then_fail)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, idea_of(run).status) == (RunStatus.FAILED, RunEnd.CANCELLED, IdeaStatus.OPEN)
        assert [w.state for w in s.scalars(select(AgentSession).where(working_on(run_id)))] == [
            AgentSessionState.NEVER_WORKED
        ]
        assert s.scalars(select(Event).where(Event.kind == 'run.relaunched')).all() == []


def test_a_cancelled_run_whose_implementer_ends_by_itself_first_closes_as_cancelled(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The daemon ends a cancelled implementer on its next tick; one that ends before that does not requeue its idea."""
    run_id = make_run(db, campaign)
    with db.session() as s:
        stop.request_cancel(s, s.get_one(Run, run_id), System.HUMAN, ending=Ending.NOW)
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, idea_of(run).status) == (RunStatus.FAILED, RunEnd.CANCELLED, IdeaStatus.OPEN)
        assert [is_best(s, c) for c in s.scalars(select(Candidate).where(Candidate.run_id == run_id))] == [True]


@pytest.mark.usefixtures('daemon_api')
def test_a_relaunched_implementer_signs_as_itself_and_its_run_end_without_a_commit_counts(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A relaunched implementer gets a brief that names it, not the dead implementer, and its own run end counts."""
    run_id = make_run(db, campaign)
    idea_id = claimed_id(run_row(db, run_id))
    note = f"INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea', {idea_id}, '$me', 'note', 'no effect')"
    me = 'me=$(grep -o "sign what you write as .implementer#[0-9]*" ../brief.md | grep -o "implementer#[0-9]*")'
    write_toml(Path(campaign.path), f'#!/bin/sh\n{me}\n{PILOOP_HINT} sql "{note}"\n{END_RUN}')
    reload_campaign(db, campaign)
    start = mocker.patch.object(runtime, 'start', wraps=runtime.start)
    start.side_effect = [RuntimeError('no pickup'), DEFAULT]
    ex = controller(db, runtime, tmp_path)
    execute(ex, db, run_id)
    execute(ex, db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    brief = brief_of(db, run.id)
    with db.session() as s:
        dead, replacement = s.scalars(select(AgentSession.id).where(working_on(run_id)).order_by(AgentSession.id)).all()
        [author] = s.scalars(select(Comment.author).where(Comment.body == 'no effect')).all()
        assert author == f'implementer#{replacement}'
        assert f'implementer#{dead}' not in brief and f'You are implementer#{replacement},' in brief
        assert s.get_one(Idea, idea_id).status is IdeaStatus.OPEN


@pytest.mark.usefixtures('daemon_api')
@pytest.mark.parametrize(
    ('work', 'ended'), [('commit', RunEnd.FINISHED), ('note', RunEnd.GAVE_UP), ('end', RunEnd.FINISHED)]
)
def test_a_fast_implementer_whose_tokens_are_unknown_is_no_startup_death_when_it_committed_wrote_or_ended_its_run(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work: str,
    ended: RunEnd,
) -> None:
    """An implementer whose tokens are unknown is not a startup death when it committed, wrote a comment, or ended its run.

    The runtime may report no session for an implementer, so its tokens cannot be read. Work done within the startup window
    proves that it started. Its clean exit ends its run: finished with a commit, in a campaign that scores every
    version after the run as this one does, and given up with only a note.
    """
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))  # no session file: the tokens are unknown
    run_id = make_run(db, campaign)
    idea_id = claimed_id(run_row(db, run_id))
    note = f"INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea', {idea_id}, '$me', 'note', 'no effect')"
    me = 'me=$(grep -o "sign what you write as .implementer#[0-9]*" ../brief.md | grep -o "implementer#[0-9]*")'
    bodies = {
        'commit': HARNESS_APPEND,
        'note': f'#!/bin/sh\n{me}\n{PILOOP_HINT} sql "{note}"\n',
        'end': f'#!/bin/sh\n{END_RUN}',
    }
    write_toml(Path(campaign.path), bodies[work])
    script = load_campaign_config(Path(campaign.path)).harness['script'].model_dump(mode='json')
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    set_config(db, campaign, plugins={'harness': {'script': {**script, **counted}}})
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.end, run.error) == (ended, None)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(AgentSession).where(working_on(run_id))) == 1, 'no relaunch'
        assert not s.scalars(select(Event).where(has(RunRelaunched) | has(StartupAnomaly))).all()


@pytest.mark.parametrize('edit', ['echo line >> solve.txt', 'echo note > new.txt'])
def test_a_fast_implementer_that_left_uncommitted_work_started_and_its_work_is_recovered(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: str
) -> None:
    """An implementer that changed its worktree worked, however fast it ended without a commit or a comment.

    Its change is recovered as for any other implementer. A relaunch would remove the worktree, and the change with it.
    """
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    write_toml(Path(campaign.path), f'#!/bin/sh\n{edit}\n')
    script = load_campaign_config(Path(campaign.path)).harness['script'].model_dump(mode='json')
    counted = {'headless': ['--session-id', '{session_id}', '{prompt}'], **READS_PI}
    set_config(db, campaign, plugins={'harness': {'script': {**script, **counted}}})
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (run_row(db, run_id).end, run_row(db, run_id).error) == (RunEnd.GAVE_UP, None)
    [(summary, status, *_)] = scored(db, run_id)
    assert (summary, status) == (UNCOMMITTED, Origin.RECOVERED)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(AgentSession).where(working_on(run_id))) == 1, 'no relaunch'
        assert not s.scalars(select(Event).where(has(StartupAnomaly))).all()


def test_a_startup_death_during_a_stop_leaves_the_run_to_the_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A startup death during a stop does not relaunch the run, because the stop fails the open runs itself."""
    ex = controller(db, runtime, tmp_path)

    def stopped_meanwhile(spec: object) -> Handle:
        ex.launcher.cancelled.set()  # what ``cancel`` sets; it takes the lock this start holds
        raise RuntimeError('no pickup')

    mocker.patch.object(runtime, 'start', side_effect=stopped_meanwhile)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    with db.session() as s:
        assert s.get_one(Run, run_id).status is RunStatus.RUNNING, 'left for the stop to fail'
        assert s.scalar(select(func.count()).select_from(AgentSession).where(working_on(run_id))) == 1


@pytest.mark.usefixtures('daemon_api')
def test_a_run_ended_as_submitted_without_a_commit_is_a_result(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run whose worker posts a note, commits nothing and ends it as submitted succeeds, and its idea is done.

    Some plans, such as an audit, produce a note and no code.
    """
    run_id = make_run(db, campaign)
    with db.session() as s:
        idea_id, agent_session_id = claimed_id(run_row(db, run_id)), s.scalars(select(AgentSession.id)).one()
    note = f"INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('idea', {idea_id}, 'implementer#{agent_session_id}', 'note', 'arm B: no effect')"
    write_toml(Path(campaign.path), f'#!/bin/sh\n{PILOOP_HINT} sql "{note}"\n{END_RUN}')
    reload_campaign(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    with db.session() as s:
        assert s.get_one(Idea, idea_id).status is IdeaStatus.OPEN, 'no score, so not done'
        assert [c.body for c in s.scalars(select(Comment).where(Comment.author == 'daemon'))] == [
            f'open: run#{run_id} gave the idea back; it finished, but nothing that it saved has a score'
        ]


TIMEOUT = 'implementer timeout after 1s'
SUBMITTED = RunEndRequested(how=RunEnding.SUBMITTED)
GAVE_UP = RunEndRequested(how=RunEnding.GAVE_UP, reason='the window does not fit in memory')


@pytest.mark.parametrize(
    ('facts', 'expected'),
    [
        # one case per case of run_outcome, in order
        pytest.param(RunFacts(ending=SUBMITTED), (RunEnd.FINISHED, None), id='submitted'),
        pytest.param(RunFacts(ending=GAVE_UP), (RunEnd.GAVE_UP, GAVE_UP.reason), id='gave-up'),
        pytest.param(RunFacts(cancelled=True), (RunEnd.CANCELLED, None), id='cancel'),
        pytest.param(RunFacts(ended=True), (RunEnd.CAMPAIGN_STOPPED, None), id='stop'),
        pytest.param(
            RunFacts(timed_out=True, stop_cut=True, error=TIMEOUT), (RunEnd.CAMPAIGN_STOPPED, None), id='stop-cut'
        ),
        pytest.param(RunFacts(timed_out=True, error=TIMEOUT), (RunEnd.TIMED_OUT, TIMEOUT), id='timeout'),
        pytest.param(RunFacts(error='implementer exit 1'), (RunEnd.FAILED, 'implementer exit 1'), id='error'),
        pytest.param(RunFacts(unsaved='refused'), (RunEnd.FAILED, f'{UNSAVED}: refused'), id='unsaved'),
        pytest.param(RunFacts(submitted=True), (RunEnd.FINISHED, None), id='clean-exit-submitted'),
        pytest.param(RunFacts(), (RunEnd.GAVE_UP, None), id='clean-exit-nothing-submitted'),
        # the overlaps that fix the order: each pair of neighbouring rows, and a run that ends after its worker ended it
        pytest.param(RunFacts(ending=SUBMITTED, ended=True), (RunEnd.FINISHED, None), id='submitted-stop'),
        pytest.param(RunFacts(ending=SUBMITTED, cancelled=True), (RunEnd.FINISHED, None), id='submitted-cancel'),
        pytest.param(
            RunFacts(ending=SUBMITTED, timed_out=True, error=TIMEOUT), (RunEnd.FINISHED, None), id='submitted-timeout'
        ),
        pytest.param(
            RunFacts(ending=GAVE_UP, error='implementer exit 1'), (RunEnd.GAVE_UP, GAVE_UP.reason), id='gave-up-error'
        ),
        pytest.param(RunFacts(ended=True, cancelled=True), (RunEnd.CANCELLED, None), id='cancel-stop'),
        pytest.param(
            RunFacts(ended=True, timed_out=True, error=TIMEOUT), (RunEnd.CAMPAIGN_STOPPED, None), id='stop-timeout'
        ),
        pytest.param(
            RunFacts(cancelled=True, timed_out=True, error=TIMEOUT), (RunEnd.CANCELLED, None), id='cancel-timeout'
        ),
        pytest.param(
            RunFacts(error='implementer exit 1', unsaved='refused'),
            (RunEnd.FAILED, 'implementer exit 1'),
            id='error-unsaved',
        ),
        pytest.param(
            RunFacts(unsaved='refused', submitted=True), (RunEnd.FAILED, f'{UNSAVED}: refused'), id='unsaved-submitted'
        ),
        pytest.param(
            RunFacts(ending=GAVE_UP, submitted=True), (RunEnd.GAVE_UP, GAVE_UP.reason), id='gave-up-submitted'
        ),
    ],
)
def test_how_a_run_ended_is_the_first_row_that_holds(facts: RunFacts, expected: tuple[RunEnd, str | None]) -> None:
    assert run_outcome(facts) == expected


@pytest.mark.parametrize('ended', [NotStarted('no server'), Lost('worker lost')], ids=['not-started', 'lost'])
def test_a_failed_start_or_a_lost_job_while_the_daemon_stops_leaves_the_run_to_the_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture, ended: Ended
) -> None:
    """The stop fails the open runs itself, so the run is neither relaunched nor closed as lost."""
    ex = controller(db, runtime, tmp_path)

    def stopped_meanwhile(*args: object, **kwargs: object) -> Ended:
        ex.launcher.cancel()
        return ended

    mocker.patch.object(ex.launcher, 'launch', side_effect=stopped_meanwhile)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    assert run_row(db, run_id).status is RunStatus.RUNNING, 'stop() writes the row, not the run thread'


@pytest.mark.usefixtures('daemon_api')
@pytest.mark.parametrize('timed_out', [True, False])
def test_an_implementer_that_ended_its_run_has_finished_it_however_its_process_then_ends(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture, timed_out: bool
) -> None:
    """An implementer that committed and ended its run as submitted, then timed out or exited non-zero, finished its idea.

    Its run's end is its report that it is done, as on a daemon stop (``Scheduler._hand_off``): the run succeeds and the
    idea is ``done``, so no second run redoes the work. A job that it forgot keeps the run open to the timeout. The
    agent session row keeps how its process ended.
    """
    run_id = make_run(db, campaign)
    with db.session() as s:
        idea_id, agent_session_id = claimed_id(run_row(db, run_id)), s.scalars(select(AgentSession.id)).one()
    note = (
        'INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES'
        f" ('idea', {idea_id}, 'implementer#{agent_session_id}', 'note', 'DONE: added a line')"
    )
    end = 'touch ../found\nsleep 30 &\nwait' if timed_out else 'exit 1'
    body = f'#!/bin/sh\necho line >> solve.txt\ngit commit -qam "a line"\n{PILOOP_HINT} sql "{note}"\n{END_RUN}{end}\n'
    write_toml(Path(campaign.path), body)
    reload_campaign(db, campaign)
    wait = runtime.wait

    def time_out_after_the_end(handle: Handle, deadline: Deadline) -> AgentExit:
        wait_until((tmp_path / 'runs' / str(run_id) / 'found').exists)
        return wait(handle, Deadline.after(0.5))

    if timed_out:  # a loaded host can take longer than any short timeout to end the run
        mocker.patch.object(runtime, 'wait', side_effect=time_out_after_the_end)
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    with db.session() as s:
        assert s.get_one(Idea, idea_id).status is IdeaStatus.DONE, 'not retried: its commit has a score'
        assert s.get_one(AgentSession, agent_session_id).error == (
            'agent timeout after 0.5s'
            if timed_out
            else f'agent exit 1: 1 row(s) changed, last insert id 1 | run#{run_id} ended as submitted: the daemon ends this session in a moment'
        ), 'an exit says its last output'
        assert [is_best(s, c) for c in s.scalars(select(Candidate).where(Candidate.run_id == run_id))] == [True]


@pytest.mark.usefixtures('daemon_api')
def test_an_open_escalation_holds_no_run_whose_worker_ends_it_as_its_verb_says(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An escalation asks the human and changes nothing else: a run whose worker asked, then ended it, ends so."""
    run_id = make_run(db, campaign)
    with db.session() as s:
        idea_id, agent_session_id = claimed_id(run_row(db, run_id)), s.scalars(select(AgentSession.id)).one()
    ask = escalation_sql('idea', idea_id, f'implementer#{agent_session_id}')
    write_toml(
        Path(campaign.path),
        f'#!/bin/sh\n{PILOOP_HINT} sql "{ask}"\necho line >> solve.txt\ngit commit -qam "a line"\n{END_RUN}',
        implementer_timeout=600,  # `piloop sql` and `piloop run end` before the deadline, also on a loaded host
    )
    reload_campaign(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        assert s.scalars(select(Comment.acked_at).where(Comment.kind == ESCALATION)).all() == [None], 'still asked'
        run = s.get_one(Run, run_id)
        assert (run.status, run.end, run.error) == (RunStatus.SUCCEEDED, RunEnd.FINISHED, None)
        assert s.get_one(Idea, idea_id).status is IdeaStatus.DONE
        assert s.scalars(select(Comment).where(Comment.author == 'daemon', Comment.target_id == idea_id)).all() == []


def test_uncommitted_changes_are_saved_on_a_machine_with_no_git_identity(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, no_git_identity: None
) -> None:
    """A fresh worker has no ``user.*``: the save of what the implementer left uncommitted commits anyway."""
    write_toml(Path(campaign.path), HARNESS_FORGETS_COMMIT)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (run_row(db, run_id).end, run_row(db, run_id).error) == (RunEnd.GAVE_UP, None)
    [(summary, status, *_)] = scored(db, run_id)
    assert (summary, status) == (UNCOMMITTED, Origin.RECOVERED)


def test_a_save_that_fails_fails_the_run_and_keeps_the_unsaved_changes_in_the_cas(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A save that git refuses is never silent: ``WorkUnsaved`` on the implementer, and the run says so."""
    write_toml(Path(campaign.path), HARNESS_FORGETS_COMMIT)
    reload_campaign(db, campaign)
    hook = Path(campaign.path) / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\necho refused >&2\nexit 1\n')
    hook.chmod(0o755)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert run.status is RunStatus.FAILED and run.error is not None and run.error.startswith(UNSAVED)
    assert not scored(db, run_id)
    with db.session() as s:
        [(_, unsaved)] = in_order(s, WorkUnsaved)
    assert 'refused' in unsaved.error
    patch = home_cas(db).get(unsaved.blob)
    assert patch is not None and b'+line' in patch


@pytest.mark.usefixtures('daemon_api')
def test_an_implementers_own_commit_is_by_the_implementer_on_a_machine_with_no_git_identity(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, no_git_identity: None
) -> None:
    """Its job's environment names it: the commit is the implementer's, and no git config is written for it."""
    write_toml(Path(campaign.path), '#!/bin/sh\necho line >> solve.txt\ngit commit -qam "a line"\n' + END_RUN)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        session = s.scalars(select(AgentSession.id).where(working_on(run_id))).one()
    [e] = candidates(db, run_id)
    repo = Repo(campaign.path)
    assert (e.summary, run_row(db, run_id).status) == ('a line', RunStatus.SUCCEEDED)
    assert by(repo, e.revision) == (f'implementer#{session} <{PILOOP.email}>',) * 2
    assert 'user.name' not in repo.git.config('--list')


@pytest.mark.usefixtures('daemon_api')
def test_uncommitted_changes_of_a_run_that_was_not_ended_are_kept_but_not_an_attempt_that_finished(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An implementer that leaves uncommitted changes and does not end its run did not finish its idea.

    The daemon saves the changes, but the run fails and the idea is open again. A note changes nothing: only
    ``piloop run end`` makes the same run a success.
    """
    write_toml(Path(campaign.path), HARNESS_FORGETS_COMMIT)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    run = run_row(db, run_id)
    assert (run.end, run.error) == (RunEnd.GAVE_UP, None)
    [(summary, status, *_)] = scored(db, run_id)
    assert (summary, status) == (UNCOMMITTED, Origin.RECOVERED), 'what it left is kept, not scored'
    with db.session() as s:
        assert s.get_one(Idea, run.idea_id).status is IdeaStatus.OPEN

    run_id = make_run(db, campaign, title='reported')
    with db.session() as s:
        idea_id, agent_session_id = (
            claimed_id(run_row(db, run_id)),
            s.scalars(select(AgentSession.id).where(working_on(run_id))).one(),
        )
        s.add(
            Comment(
                target=TargetRef(RUN, run_id),
                author=f'implementer#{agent_session_id}',
                kind='note',
                body='x',
            )
        )
        s.add(Comment(target=TargetRef(IDEA, idea_id), author='orchestrator#9', kind='note', body='y'))
        s.commit()
    write_toml(Path(campaign.path), '#!/bin/sh\necho other >> solve.txt\n')
    reload_campaign(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert (run_row(db, run_id).end, run_row(db, run_id).error) == (RunEnd.GAVE_UP, None), 'its notes end no run'

    run_id = make_run(db, campaign, title='ended')
    write_toml(Path(campaign.path), '#!/bin/sh\necho more >> solve.txt\n' + END_RUN)
    reload_campaign(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert run_row(db, run_id).status is RunStatus.SUCCEEDED, 'its worker ended it as submitted'


def test_a_cancelled_executor_leaves_the_bookkeeping_to_the_scheduler(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    ex = controller(db, runtime, tmp_path, tracer)
    queued = make_run(db, campaign)
    ex.launcher.cancel()
    execute(ex, db, queued)
    assert run_row(db, queued).status is RunStatus.QUEUED, 'not even started'
    assert dict(span_named(spans, 'piloop.run').attributes or {}) == {
        'run': queued,
        'status': 'failed',
        'end': 'daemon_stopped',
    }, 'what the stop writes on the row'


def test_an_implementer_dying_after_cancel_is_not_a_run_failure(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    ex = controller(db, runtime, tmp_path)

    def tab_closed(handle: Handle, timeout: float) -> AgentExit:
        ex.launcher.cancel()
        raise RuntimeError('agent_not_found')

    mocker.patch.object(runtime, 'wait', side_effect=tab_closed)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.RUNNING, None), 'stop() writes the row, not the dying thread'
    workers_of(ex).worker.join(10)  # it removes the worktree once its report is out
    assert not (tmp_path / 'runs' / str(run_id) / 'worktree').exists()


def test_a_stop_between_the_implementers_end_and_the_runs_close_leaves_the_row_to_the_scheduler(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    ex = controller(db, runtime, tmp_path)

    def stopped_meanwhile(*args: object) -> list[str]:
        ex.launcher.cancel()
        raise RuntimeError('worktree pulled away')

    mocker.patch.object(RunController, '_received', side_effect=stopped_meanwhile)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    run = run_row(db, run_id)
    assert (run.status, run.error) == (RunStatus.RUNNING, None), 'stop() writes the row, not the dying thread'
    with db.session() as s:
        assert s.get_one(Idea, run.idea_id).status is IdeaStatus.CLAIMED, 'and returns the idea'


def test_an_evaluator_spawned_after_cancel_is_killed_at_once(
    db: Database, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    ex = controller(db, runtime, tmp_path)
    ex.stop(0)
    proc = subprocess.Popen(['sleep', '30'], start_new_session=True)
    worker = workers_of(ex).worker
    worker._register(1, None, agent=False)  # the evaluation's job, before its evaluator starts
    evaluation_jobs._track(
        _Control(worker, 1), proc
    )  # what run_evaluate calls right after Popen: the stop may have won meanwhile
    assert proc.wait(timeout=2) == -9


def test_a_commit_that_reverts_to_a_known_tree_is_that_candidate_and_is_not_measured_again(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    write_toml(Path(campaign.path), HARNESS_TRY_AND_REVERT)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path, tracer), db, run_id)
    [tried] = candidates(db, run_id)
    assert (tried.summary, tried.best, tried.metrics['score']) == ('try', True, 2.0)
    base = baseline(db, campaign.id)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Candidate)) == 2, 'the revert is the baseline, not a third row'
        assert len(s.get_one(Candidate, base.id).measurements) == 1, 'and the baseline was not measured again'
        kinds = [ev.kind for ev in s.scalars(select(Event)).all()]
    assert kinds.count('measurement.recorded') == 1 and 'candidate.scored' in kinds
    known = [sp for sp in spans.get_finished_spans() if sp.name == 'piloop.candidate' and sp.attributes]
    assert [sp.attributes.get('status') for sp in known if sp.attributes] == snapshot(
        [Measuring.MEASURED, Measuring.MEASURED]
    )
    repo = Repo(campaign.path)
    assert len([r for r in repo.refs if r.path.startswith('refs/piloop/')]) == 2, 'both commits are still pinned'


def test_a_changed_evaluator_measures_again_and_old_numbers_do_not_count(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [added] = candidates(db, run_id)
    assert added.metrics['score'] == 2.0
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_TRY_AND_REVERT, evaluate=doubled)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        before = proj.evaluator_id
        sync(s, proj)
        s.commit()
        assert proj.evaluator_id != before, 'a new evaluate command is a new evaluator'
        assert [c.id for c in unready(s, proj)] == [1, added.id], (
            'nothing is measured under the new evaluator yet: runs wait for the baseline and the best under the old one'
        )
    second = make_run(db, campaign, title='again')
    with db.session() as s:
        s.get_one(Run, second).base_candidate_id = added.id
        s.commit()
    execute(controller(db, runtime, tmp_path), db, second)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        tried = s.scalars(select(Candidate).where(Candidate.run_id == second)).one()
        assert [m.metrics['score'] for m in tried.measurements] == [6.0], 'measured under the doubled evaluator'
        base = s.get_one(Candidate, added.id)
        assert [m.metrics['score'] for m in base.measurements] == [2.0, 4.0], (
            'the revert to the base is measured under the new evaluator, which had not measured it'
        )
        assert best_of(s, proj) == tried, '6 against 4: the old 2 does not count'


def test_a_config_change_during_a_run_scores_it_under_the_new_evaluator(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``piloop init`` during a long run: the run finishing must not point the campaign back at the old evaluator."""
    gate = Gate()
    monkeypatch.setattr(runtime, 'start', gate.wrap(runtime.start))
    run_id = make_run(db, campaign)
    thread = threading.Thread(target=execute, args=(controller(db, runtime, tmp_path), db, run_id))
    thread.start()
    assert gate.entered.wait(10), 'the implementer is about to start; the run holds the old config'
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    write_toml(Path(campaign.path), HARNESS_APPEND, evaluate=doubled)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        sync(s, proj)
        s.commit()
        new_evaluator = proj.evaluator_id
    gate.release.set()
    thread.join(30)
    [added] = candidates(db, run_id)
    with db.session() as s:
        assert s.get_one(Campaign, campaign.id).evaluator_id == new_evaluator
        assert [m.evaluator_id for m in s.get_one(Candidate, added.id).measurements] == [new_evaluator]
        assert added.metrics['score'] == 4.0, 'measured under the doubled evaluator'


def test_a_config_change_while_a_run_scores_measures_its_next_commits_under_the_new_evaluator(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``piloop init`` between two commits' evaluations: the second is measured and scored under the new evaluator."""
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]

    def then_init[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
        def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
            done = fn(*args, **kwargs)
            write_toml(Path(campaign.path), HARNESS_TWO_COMMITS, evaluate=doubled)
            with db.session() as s:
                sync(s, s.get_one(Campaign, campaign.id))
                s.commit()
            return done

        return measured

    monkeypatch.setattr(ex, '_measure', then_init(ex._measure))
    execute(ex, db, run_id)
    with db.session() as s:
        new_evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        first, second = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        assert [(m.evaluator_id == new_evaluator, m.metrics['score']) for m in first.measurements] == [
            (False, 2.0),
            (True, 4.0),
        ], 'the evaluator changed while it was scored: it is measured again under the new one'
        assert [(m.evaluator_id, m.metrics['score']) for m in second.measurements] == [(new_evaluator, 6.0)], (
            'measured under the evaluator of the config it is scored in'
        )
        assert [scored.score for e, scored in in_order(s, CandidateScored) if e.target_id == second.id] == [6.0]
        assert best_of(s, s.get_one(Campaign, campaign.id)) == second


def test_a_config_change_while_a_commit_is_measured_scores_it_under_the_new_evaluator(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``piloop init`` while the run's first evaluation starts: no commit is scored on the old evaluator."""
    write_toml(Path(campaign.path), HARNESS_TWO_COMMITS)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    doubled = ['sh', '-c', reports(score='$(( 2 * $(wc -l < solve.txt) ))', size='1')]
    inits: list[None] = []

    def init_during[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
        def measuring(*args: P.args, **kwargs: P.kwargs) -> Measuring:
            if not inits:
                inits.append(None)
                write_toml(Path(campaign.path), HARNESS_TWO_COMMITS, evaluate=doubled)
                with db.session() as s:
                    sync(s, s.get_one(Campaign, campaign.id))
                    s.commit()
            return fn(*args, **kwargs)

        return measuring

    monkeypatch.setattr(ex, '_measure', init_during(ex._measure))
    execute(ex, db, run_id)
    with db.session() as s:
        new = s.get_one(Campaign, campaign.id).evaluator_id
        first, second = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()

        def under_new(c: CandidateId) -> list[float]:
            return [m.metrics['score'] for m in s.get_one(Candidate, c).measurements if m.evaluator_id == new]

        assert [under_new(c) for c in (first.id, second.id)] == [[4.0], [6.0]], (
            'both commits are measured under the evaluator that scores them'
        )
        scores = [(e.target_id, scored.score) for e, scored in in_order(s, CandidateScored)]
        assert scores == [(first.id, 4.0), (second.id, 6.0)], 'each scored under the new evaluator'


SPLIT_UNITS = [
    'sh',
    '-c',
    'L=$(wc -l < solve.txt); printf \'{"metrics": {"score": %d, "size": %d}, "units": {"score": {"a": %d, "b": %d}}}\\n\''
    ' $L $(wc -c < solve.txt) $((2*L-1)) $((1-L)) >> "$PILOOP_RESULT_FILE"',
]
"""score = lines, split over two units so one more line is +2 on ``a`` and -1 on ``b``: up, but not significantly."""


def set_config(db: Database, campaign: Campaign, **overrides: object) -> None:
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), **overrides)
        s.commit()


def test_per_unit_results_live_outside_the_database(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    units_dir = tmp_path / 'units'
    set_config(db, campaign, evaluate=SPLIT_UNITS)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        c = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).one()
        [m] = c.measurements
        assert m.metrics.keys() == {'score', 'size'}, 'per-unit results are not a column any agent can SELECT'
    assert json.loads((units_dir / f'{m.id}.json').read_text()) == {'score': {'a': 3, 'b': -1}}
    assert sorted(p.name for p in units_dir.iterdir()) == [f'{m.id}.json'], 'no pending file is left behind'


def test_guardrails_are_enforced_by_the_daemon_even_after_the_campaign_stopped(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The best rule enforces a guardrail floor, also on a run still in flight after the campaign stopped."""
    metrics = [
        {'name': 'score', 'direction': 'higher', 'role': 'primary'},
        {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 100, 'floor': 8},
    ]
    set_config(db, campaign, metrics=metrics)
    run_id = make_run(db, campaign)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        proj.status, proj.stopped_reason = CampaignStatus.STOPPED, 'max_runs: 1'
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        c = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).one()
        assert (is_best(s, c), told_of(s, c.id)) == snapshot(
            (False, 'score 2 over 1 eval: it may not be best: floor: size 10 past its floor 8')
        )


def test_units_of_a_candidate_are_the_mean_per_unit_over_its_measurements(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    with db.session() as s:
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator_id is not None
        c = Candidate(campaign_id=campaign.id, tree_sha='t', revision='t')
        s.add(c)
        s.flush()
        rows = [Measurement(candidate_id=c.id, evaluator_id=evaluator_id, metrics={'score': 1.0}) for _ in range(4)]
        rows[3].error = 'crashed'
        s.add_all(rows)
        s.flush()
        save_units(tmp_path, rows[0].id, {'score': {'a': 1.0, 'b': 0.0}})
        save_units(tmp_path, rows[1].id, {'score': {'a': 3.0, 'b': 0.0}})
        save_units(tmp_path, rows[3].id, {'score': {'a': 100.0, 'b': 0.0}})
        (tmp_path / f'{rows[2].id}.json').write_text('{"score": {"a": 50}}')
        save_units(tmp_path, rows[2].id, {})
        assert units(c, evaluator_id, tmp_path) == {'score': {'a': 2.0, 'b': 0.0}}, (
            'a crashed measurement does not count, and one without units leaves no stale file behind'
        )


HARNESS_SCAFFOLD_THEN_WORK = (
    '#!/bin/sh\necho a >> solve.txt\ngit commit -qam scaffold -m "piloop-eval: SKIP"\n'
    'echo b >> solve.txt\ngit commit -qam work\n'
)


def scored(db: Database, run_id: RunId) -> list[tuple[str | None, str, str | None, int, int | None]]:
    """``(summary, status, told, measurements, parent)`` of each candidate the run produced, in order.

    The status is ``best`` for the best of its track now, ``scored`` for another one in the pool, and the origin of one
    that never enters the pool (``UNSCORED``). ``told`` is what its last score told its worker, or None."""
    with db.session() as s:
        rows = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        return [
            (
                c.summary,
                c.origin if c.origin in UNSCORED else 'best' if is_best(s, c) else 'scored',
                told_of(s, c.id),
                len(c.measurements),
                c.parent_id,
            )
            for c in rows
        ]


def test_a_commit_marked_skip_is_a_candidate_without_an_evaluation_that_never_enters_the_pool(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_SCAFFOLD_THEN_WORK)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        first = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).first()
        assert first is not None
        skip_id = first.id
    assert scored(db, run_id) == snapshot(
        [
            ('scaffold', Origin.SKIPPED, None, 0, 1),
            ('work', 'best', 'score 3 over 1 eval: the best of the score track now, rank 1 of 2', 1, 2),
        ]
    ), 'the next commit descends from the scaffold'
    with db.session() as s:
        assert [e for _, e in in_order(s, OriginChanged)] == [OriginChanged(old=Origin.RUN, new=Origin.SKIPPED)]
        assert [e.target_id for e, _ in in_order(s, CandidateScored)] == [skip_id + 1], 'a skipped commit is not scored'


def test_a_skipped_tree_reached_again_without_the_trailer_is_measured_and_scored(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A revert to a skipped tree is measured and scored like a new candidate.

    The run makes a scaffold commit marked skip, then a work commit that is best, then reverts to the scaffold's tree.
    """
    write_toml(Path(campaign.path), HARNESS_SCAFFOLD_THEN_WORK + 'git revert --no-edit HEAD >/dev/null\n')
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [scaffold, work] = scored(db, run_id)
    assert scaffold[1:4] == snapshot(
        ('scored', 'score 2 over 1 eval: rank 2 of 3; vs parent +1, vs best candidate#3 -1', 1)
    )
    assert work[1] == 'best'


def test_a_skip_commit_on_a_known_tree_leaves_it_unmeasured(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    harness = HARNESS_TRY_AND_REVERT + 'git commit -q --amend -m "back" -m "Piloop-Eval: skip"\n'
    write_toml(Path(campaign.path), harness)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        base = s.get_one(Candidate, baseline(db, campaign.id).id)
        assert (base.origin, len(base.measurements)) == (Origin.BASELINE, 1)


def test_dirt_after_a_skip_commit_is_committed_on_its_own_and_evaluated(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    harness = '#!/bin/sh\necho a >> solve.txt\ngit commit -qam scaffold -m "Piloop-Eval: skip"\necho b >> solve.txt\n'
    write_toml(Path(campaign.path), harness)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert [(c[0], c[1], c[3]) for c in scored(db, run_id)] == [
        ('scaffold', Origin.SKIPPED, 0),
        (UNCOMMITTED, Origin.RECOVERED, 0),
    ], 'the work after a skip commit is recovered on its own: never amended into the commit'


def test_a_commit_that_moves_no_deciding_metric_is_level_with_its_parent(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), '#!/bin/sh\necho x > notes.txt\ngit add notes.txt\ngit commit -qm notes\n')
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [(_, status, told, _, _)] = scored(db, run_id)
    assert (status, told) == snapshot(
        (
            'scored',
            'score 1 over 1 eval: rank 2 of 2; vs parent +0 (within noise), vs best candidate#1 +0 (within noise)',
        )
    ), 'a tie: the older one is best'


def scored_elsewhere(
    db: Database,
    campaign: Campaign,
    summary: str,
    files: dict[str, str],
    metrics: dict[str, float],
    *,
    on: int = 1,
    run_id: RunId | None = None,
) -> CandidateId:
    """Add a candidate made outside any run, scored into the pool.

    It writes ``files`` on a commit off candidate ``on`` and is measured once as ``metrics``. To a run that started
    before it, it looks like another run's candidate. With ``run_id``, it is that run's candidate.
    """
    repo = Repo(campaign.path)
    with db.session() as s:
        parent = s.get_one(Candidate, on)
        repo.git.checkout('-q', '--detach', parent.revision)
        for name, text in files.items():
            (Path(campaign.path) / name).write_text(text)
        repo.git.add('-A')
        repo.git.commit('-qm', summary)
        sha = repo.head.commit.hexsha
        repo.git.checkout('-q', 'main')
        git_pin(repo, f'refs/piloop/demo/elsewhere/{summary.replace(" ", "-")}', sha)
        candidate, _ = resolve(
            s, campaign.id, GitRepository(repo, PILOOP).version(sha), parent=parent, run_id=run_id, origin=Origin.RUN
        )
        evaluator_id = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator_id is not None
        s.add(Measurement(candidate_id=candidate.id, evaluator_id=evaluator_id, metrics=metrics))
        s.commit()
        return candidate.id


def test_two_sessions_that_register_one_new_tree_share_one_candidate(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second session looked before the first committed, so its insert conflicts."""
    repo = Repo(campaign.path)
    (Path(campaign.path) / 'solve.txt').write_text('base\nsame\n')
    repo.git.commit('-qam', 'same')
    sha = repo.head.commit.hexsha
    parent = baseline(db, campaign.id)
    with db.session() as a, db.session() as b:
        looked = b.scalar

        def stale(*args: object, **kwargs: object) -> object:
            monkeypatch.setattr(b, 'scalar', looked)
            return None

        monkeypatch.setattr(b, 'scalar', stale)
        first, _ = resolve(
            a, campaign.id, GitRepository(repo, PILOOP).version(sha), parent=parent, run_id=None, origin=Origin.RUN
        )
        a.commit()
        found, created = resolve(
            b, campaign.id, GitRepository(repo, PILOOP).version(sha), parent=parent, run_id=None, origin=Origin.RUN
        )
        b.commit()
        assert (found.id, created) == (first.id, False)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Candidate).where(Candidate.tree_sha == first.tree_sha)) == 1


def test_a_known_tree_that_another_run_reaches_again_is_not_measured_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The same tree again says nothing new about it: it keeps its score, and stays the best."""
    ex = controller(db, runtime, tmp_path)
    first = make_run(db, campaign)
    execute(ex, db, first)
    [known] = candidates(db, first)
    execute(ex, db, make_run(db, campaign, title='a compose that reproduces the tree'))
    with db.session() as s:
        again = s.get_one(Candidate, known.id)
        assert (len(again.measurements), again.run_id) == (1, first), 'scored with the measurement it has'
        assert best_of(s, s.get_one(Campaign, campaign.id)) == again


def test_a_merge_commit_descends_from_its_first_parent_the_runs_base(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run's commits are its first-parent history, so a merge's parent is the run's base.

    The merged-in line is not the run's, and its commits are known candidates already.
    """
    metrics = {'score': 1.0, 'size': 5.0}
    side = scored_elsewhere(db, campaign, 'notes', {'notes.txt': 'n\n'}, metrics)
    trunk = scored_elsewhere(db, campaign, 'docs', {'docs.txt': 'd\n'}, metrics)
    with db.session() as s:
        side_sha = s.get_one(Candidate, side).revision
    write_toml(Path(campaign.path), f'#!/bin/sh\ngit merge -q --no-ff --no-edit {side_sha}\n')
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    with db.session() as s:
        s.get_one(Run, run_id).base_candidate_id = trunk
        s.commit()
    execute(controller(db, runtime, tmp_path), db, run_id)
    [(_, _, _, measurements, parent)] = scored(db, run_id)
    assert (measurements, parent) == (1, trunk)
    with db.session() as s:
        assert len(s.get_one(Candidate, side).measurements) == 1, 'the merged line is not measured again'


def cancelling(db: Database, measure: Callable[..., Measuring], cancelled: CandidateId) -> Callable[..., Measuring]:
    """``measure``, but what someone does with ``piloop eval cancel`` to the evaluation of ``cancelled``."""

    def measured(*args: object, **kwargs: object) -> Measuring:
        if args[4] != cancelled:
            return measure(*args, **kwargs)
        with db.session() as s:
            candidate = s.get_one(Candidate, cancelled)
            evaluator = s.get_one(Campaign, candidate.campaign_id).evaluator_id
            assert evaluator is not None
            queue.request(s, cancelled, evaluator)
            assert queue.cancel(s, cancelled, System.HUMAN) == 1
            s.commit()
        return Measuring.CANCELLED

    return measured


def test_a_cancelled_evaluation_leaves_its_candidate_skipped_also_when_the_run_resumes(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    measure = ex._measure
    monkeypatch.setattr(ex, '_measure', cancelling(db, measure, CandidateId(2)))
    execute(ex, db, run_id)
    monkeypatch.setattr(ex, '_measure', measure)
    ex.resume(run_id)
    with db.session() as s:
        skipped = s.get_one(Candidate, CandidateId(2))
        assert (skipped.origin, told_of(s, skipped.id), skipped.measurements) == (Origin.SKIPPED, None, [])


def test_a_tree_two_runs_reach_is_never_scored_against_itself(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree that two runs reach is never compared with itself.

    The second run reaches the tree while the first run's evaluation is pending. The first run's score then makes the
    tree the best, so the second run's evaluation adds to its mean, and its worker hears that it is the best.
    """
    ex = controller(db, runtime, tmp_path)
    first = make_run(db, campaign)
    execute(ex, db, first)
    [kept] = candidates(db, first)
    assert kept.best
    with db.session() as s:  # rewind to before the first run's measurement landed
        candidate = s.get_one(Candidate, kept.id)
        [measurement] = candidate.measurements
        evaluator_id = measurement.evaluator_id
        s.delete(measurement)
        s.commit()
    track = evaluation_jobs._track

    def kept_meanwhile(control: jobs.Control, proc: subprocess.Popen[bytes] | None) -> None:
        """Land the first run's measurement while the second run evaluates the same tree."""
        track(control, proc)
        if proc is None:
            with db.session() as s:
                s.add(Measurement(candidate_id=kept.id, evaluator_id=evaluator_id, metrics=kept.metrics))
                s.commit()

    monkeypatch.setattr(evaluation_jobs, '_track', kept_meanwhile)
    execute(ex, db, make_run(db, campaign, title='same lines'))
    with db.session() as s:
        again = s.get_one(Candidate, kept.id)
        assert (len(again.measurements), told_of(s, kept.id)) == snapshot(
            (2, 'score 2 over 2 evals: the best of the score track now, rank 1 of 2; vs parent +1')
        ), 'the best, with no delta to itself'


HARNESS_NOTED = (
    '#!/bin/sh\n: > solve.txt\n'
    'git commit -qam wipe -m "Piloop-Note: faster, though it scores less\nPiloop-Follow-Up: wipe, then refill"\n'
    'echo a >> solve.txt\necho b >> solve.txt\n'
    'git commit -qam refill -m "Piloop-Note: redundant"\n'
)


def test_a_piloop_note_is_a_note_on_its_candidate_that_files_no_follow_up_and_moves_no_score(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_toml(Path(campaign.path), HARNESS_NOTED)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    [(_, wipe, _, _, _), (_, refill, _, _, _)] = scored(db, run_id)
    assert (wipe, refill) == ('scored', 'best')
    with db.session() as s:
        notes = s.scalars(select(Comment).where(Comment.target_kind == CANDIDATE).order_by(Comment.id)).all()
        assert [(c.target_id, c.author, c.kind, c.body) for c in notes] == [
            (2, 'implementer#1', CommentKind.NOTE, 'faster, though it scores less'),
            (3, 'implementer#1', CommentKind.NOTE, 'redundant'),
        ], "each note is its implementer's, on the candidate of its commit"
        follow_ups = s.scalars(select(Idea).where(Idea.provenance.like('candidate#%'))).all()
        assert follow_ups == [], 'a Piloop-Follow-Up trailer files no seed: an implementer files none'


def test_the_best_of_one_track_must_hold_the_other_track_primaries_and_the_brief_lists_the_tracks(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A candidate on one track must stay within the floor of the other track's primary metric to be best.

    An accuracy candidate that adds one line goes past the cost track's floor on size.
    """
    path = Path(campaign.path)
    (path / 'briefs').mkdir()
    (path / 'briefs' / 'accuracy.md').write_text('# Accuracy track\nOnly add lines that are right.\n')
    tracks = {
        'metrics': [
            {'name': 'score', 'direction': 'higher', 'role': 'primary', 'track': 'accuracy', 'margin': 1},
            {'name': 'size', 'direction': 'lower', 'role': 'primary', 'track': 'cost', 'margin': 2, 'floor': 8},
        ],
        'tracks': {'accuracy': {'addendum': 'briefs/accuracy.md'}},
    }
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(path, **tracks)
        s.commit()
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(CandidateId(1))  # the baseline has a score under the new evaluator
    run_id = make_run(db, campaign, track='accuracy')
    execute(ex, db, run_id)
    [(_, status, told, _, _)] = scored(db, run_id)
    assert (status, told) == snapshot(
        (
            'scored',
            'score 2 over 1 eval: it may not be best: floor: size 10 past its floor 8; vs parent +1, vs best candidate#1 +1',
        )
    ), 'score 2 > 1, but size 10 > 8'
    brief = brief_of(db, run_id)
    assert 'Accuracy track' not in brief and 'Your track is' not in brief, 'a fresh run has no idea, so no track yet'
    assert '- Starting from candidate#1, the best: score=1.0, size=5.0 — baseline' in brief
    assert '- Tracks: accuracy (primary score, higher is better); cost (primary size' in brief


TWO_TRACKS = {
    'metrics': [
        {'name': 'score', 'direction': 'higher', 'role': 'primary', 'track': 'accuracy', 'margin': 1},
        {'name': 'size', 'direction': 'lower', 'role': 'primary', 'track': 'cost', 'margin': 100},
    ],
}
"""Two tracks: accuracy on ``score``, the lines of ``solve.txt``, and cost on ``size``, its bytes."""


def test_a_candidate_is_scored_on_its_runs_track_and_may_be_the_best_of_another(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An accuracy run that shrinks the file without a new line is no gain on accuracy, and the best on cost.

    Its worker hears where it stands on its run's track. The best of each track is a query over the whole pool.
    """
    write_toml(Path(campaign.path), "#!/bin/sh\nprintf 'a\\n' > solve.txt\ngit commit -qam 'shorter'\n")
    set_config(db, campaign, **TWO_TRACKS)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(CandidateId(1))
    run_id = make_run(db, campaign, track='accuracy')
    execute(ex, db, run_id)
    with db.session() as s:
        [c] = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).all()
        scores = [(e.track, e.score, e.best) for _, e in in_order(s, CandidateScored, Event.target_id == c.id)]
        assert scores == [('accuracy', 1.0, 1)], 'level with the baseline, the older'
        p = s.get_one(Campaign, campaign.id)
        accuracy, cost = p.config.all_tracks
        assert (best_of(s, p, accuracy), best_of(s, p, cost)) == (s.get_one(Candidate, 1), c)


def test_recovered_work_whose_tree_is_already_a_candidate_leaves_that_candidate_as_it_is(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The recovery commit is that candidate, which keeps its score; the claim of the run that left it ends."""
    write_toml(Path(campaign.path), HARNESS_APPEND)
    reload_campaign(db, campaign)
    first = make_run(db, campaign, title='commits the line')
    execute(controller(db, runtime, tmp_path), db, first)
    [known] = candidates(db, first)
    write_toml(Path(campaign.path), HARNESS_FORGETS_COMMIT)
    reload_campaign(db, campaign)
    run_id = make_run(db, campaign, title='leaves the same line')
    execute(controller(db, runtime, tmp_path), db, run_id)
    with db.session() as s:
        idea = s.get_one(Idea, claimed_id(s.get_one(Run, run_id)))
        assert idea.status is IdeaStatus.OPEN
        found = s.get_one(Candidate, known.id)
        assert (found.origin, len(found.measurements), is_best(s, found)) == (Origin.RUN, 1, True), (
            'a known tree keeps its score'
        )


def test_every_evaluator_has_the_process_runner_and_an_uninstalled_runner_is_refused(
    db: Database, campaign: Campaign
) -> None:
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, row, row.config)
        assert evaluator.runner == 'process', 'what piloop init records'
        evaluator.runner = 'lean'
        s.flush()
        with pytest.raises(ValueError, match=r"has the evaluator runner 'lean', which is not installed: process$"):
            current_evaluator(s, row, row.config)


def test_a_stop_that_comes_just_after_the_report_leaves_the_run_to_the_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The report applied before the stop, but the run had not read it yet: the stop owns the rows, as for a cut run."""
    ex = controller(db, runtime, tmp_path)
    launch = ex.launcher.launch

    def stopped_just_after(*args: Any, **kwargs: Any) -> Ended:
        ended = launch(*args, **kwargs)
        ex.launcher.cancel()
        return ended

    mocker.patch.object(ex.launcher, 'launch', side_effect=stopped_just_after)
    run_id = make_run(db, campaign)
    execute(ex, db, run_id)
    assert run_row(db, run_id).status is RunStatus.RUNNING, 'stop() writes the row, not the run thread'
    assert pinned(git.open_repo(Path(campaign.path)), f'refs/piloop/{campaign.config.name}/r{run_id}/'), (
        'its commit is kept'
    )


def test_linked_inputs_go_into_the_store_while_no_transaction_holds_the_write_lock(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The first snapshot of a large ``link`` takes minutes: the daemon's other writers must not wait on it."""
    ex = controller(db, runtime, tmp_path)
    real = ex.sources.source
    locked: list[bool] = []

    def git_source(c: Campaign, commit: Revision) -> Source:
        # another thread's write (the worker's heartbeat) ends within the wait; one of this thread's never would
        other = sqlite3.connect(db.engine.url.database or '', timeout=5)
        try:
            other.execute('BEGIN IMMEDIATE')
            locked.append(False)
        except sqlite3.OperationalError:
            locked.append(True)
        finally:
            other.close()
        return real(c, commit)

    mocker.patch.object(ex.sources, 'source', side_effect=git_source)
    ex.remeasure(baseline(db, campaign.id).id)
    assert locked == [False]


def test_an_evaluation_that_changes_a_linked_input_is_a_crash_and_the_shared_copy_is_made_again(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    """Read-only is a mode that the evaluate command can change. What it measured on inputs it changed is no
    measurement, and the next job does not get the changed copy."""
    cooks = f'chmod u+w data/x.csv && echo 0 > data/x.csv && {reports(score="9", size="1")}'
    set_config(db, campaign, link=['data'], evaluate=['sh', '-c', cooks])
    candidate = baseline(db, campaign.id).id
    ex = controller(db, runtime, tmp_path, tracer)
    ex.remeasure(candidate)
    with db.session() as s:
        measured = s.scalars(select(Measurement).where(Measurement.candidate_id == candidate)).all()[-1]
    assert (measured.metrics, measured.error) == ({'score': 9.0, 'size': 1.0}, f'{CHANGED_INPUTS}: data/x.csv'), (
        'what it printed stays next to the error, and a measurement with an error never scores'
    )
    assert [e['paths'] for e in events(spans, INPUTS_CHANGED)] == ['data/x.csv']
    set_config(
        db,
        campaign,
        link=['data'],
        evaluate=['sh', '-c', reports(score='$(cat data/x.csv)', size='1')],
    )
    ex.remeasure(candidate)
    with db.session() as s:
        measured = s.scalars(select(Measurement).where(Measurement.candidate_id == candidate)).all()[-1]
    assert measured.metrics['score'] == 1.0, 'the input as it was put in the store'
    assert len(events(spans, INPUTS_REBUILT)) == (0 if reflinks(tmp_path) else 1), 'a hard link carried the change'


def test_an_evaluation_that_imports_a_linked_module_finds_its_bytecode_current(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, tracer: Tracer
) -> None:
    """Python checks a cached ``.pyc`` against its source's modification time, and writes a new one beside it when
    they differ: a linked virtual environment's copies keep the times they were stored with."""
    data = Path(campaign.path) / 'data'
    (data / 'mod.py').write_text('X = 1\n')
    os.utime(data / 'mod.py', ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    py_compile.compile(str(data / 'mod.py'), doraise=True)
    imports = (
        "import json, os, sys; sys.path.insert(0, 'data'); import mod;"
        " open(os.environ['PILOOP_RESULT_FILE'], 'a').write(json.dumps({'metrics': {'score': 1, 'size': 1}}) + chr(10))"
    )
    set_config(
        db, campaign, link=['data'], evaluate=['env', '-u', 'PYTHONDONTWRITEBYTECODE', sys.executable, '-c', imports]
    )
    candidate = baseline(db, campaign.id).id
    controller(db, runtime, tmp_path, tracer).remeasure(candidate)
    with db.session() as s:
        measured = s.scalars(select(Measurement).where(Measurement.candidate_id == candidate)).all()[-1]
    assert (measured.metrics, measured.error) == ({'score': 1.0, 'size': 1.0}, None)


def reflinks(tmp_path: Path) -> bool:
    """Whether the filesystem of ``tmp_path`` makes copy-on-write clones."""
    (tmp_path / 'probe').write_text('x')
    try:
        with (tmp_path / 'probe').open('rb') as src, (tmp_path / 'clone').open('wb') as dst:
            fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
    except OSError:
        return False
    return True


def test_a_resumed_run_scores_the_versions_that_it_received(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop during scoring leaves the run's versions recorded: the next daemon scores them, whatever refs say."""
    run_id = make_run(db, campaign)
    ex = controller(db, runtime, tmp_path)
    measure = ex._measure

    def stopped(*args: object, **kwargs: object) -> Measuring:
        return Measuring.STOPPED  # what a stop does to the evaluation in flight

    monkeypatch.setattr(ex, '_measure', stopped)
    execute(ex, db, run_id)
    with db.session() as s:
        [waiting] = s.scalars(select(Candidate.id).where(Candidate.run_id == run_id)).all()
        assert told_of(s, waiting) is None
    repo = Repo(campaign.path)
    for ref in repo.git.for_each_ref('--format=%(refname)', f'refs/piloop/demo/r{run_id}/').split():
        repo.git.update_ref('-d', ref)
    monkeypatch.setattr(ex, '_measure', measure)
    ex.resume(run_id)
    [scored] = candidates(db, run_id)
    assert scored.best


def test_a_run_that_starts_near_the_campaigns_stop_is_told_it_ends_with_the_stop(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """The brief, ``PILOOP_DEADLINE`` and the job's timeout all said the role timeout, past a ``max_hours`` stop."""
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(
            Path(campaign.path), plugins={'stop_rule': {'max_hours': 0.002}}
        )
        s.commit()
    run_id = make_run(db, campaign)
    launched = mocker.spy(Launcher, 'launch')
    execute(controller(db, runtime, tmp_path), db, run_id)
    assert '(Unix time), 0.1 minutes after it starts.' in brief_of(db, run_id), (
        'the role timeout of 120s, cut at the stop 7.2s away'
    )
    assert launched.call_args.kwargs['timeout'] == pytest.approx(7.2, abs=1), 'from its launch to the stop'
