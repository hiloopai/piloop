"""``follow_head``: on a keep, the other running workers of a campaign hear where the head moved, and can merge it."""

from pathlib import Path

from git import Repo
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from piloop.config import load_campaign_config
from piloop.daemon.loop import Turn
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, utcnow
from piloop.db.session import Database
from piloop.db.triggers.changelog import VerdictChanged
from piloop.ids import CommentKind, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.follow_head import follow_head
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.campaign_type.kit.runs.tables import Run, RunStatus, working_on
from tests.conftest import ScriptRuntime, span_named
from tests.daemon.test_run_controller import keep_elsewhere, make_run
from tests.daemon.test_scheduler import make_scheduler


def follow(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        overrides = {'role': {'implementer': {'follow_head': 'message'}}}
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), plugins=overrides)
        s.commit()


def running(
    db: Database,
    campaign: Campaign,
    title: str,
    base: CandidateId | None = None,
    state: AgentSessionState = AgentSessionState.RUNNING,
) -> RunId:
    """A run of idea ``title`` whose worker runs now, from the baseline or from candidate ``base``."""
    run_id = make_run(db, campaign, title=title)
    with db.session() as s:
        run = s.get_one(Run, run_id)
        run.status = RunStatus.RUNNING
        if base is not None:
            run.base_candidate_id = base
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        agent.state = state
        s.commit()
    return run_id


def messages(db: Database, run_id: RunId) -> list[str]:
    with db.session() as s:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        target = TargetRef(TargetKind.AGENT_SESSION, agent.id)
        found = select(Comment.body).where(Comment.target == target, Comment.kind == CommentKind.MESSAGE)
        return list(s.scalars(found))


def tell(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        follow_head(s, s.get_one(Campaign, campaign.id))
        s.commit()


def test_a_keep_tells_each_other_running_worker_once_and_its_pin_keeps_it_reachable(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    follow(db, campaign)
    keeper, other = running(db, campaign, 'keeper'), running(db, campaign, 'other')
    kept = keep_elsewhere(
        db, campaign, 'kept', {'solve.txt': 'base\nkept\n'}, {'score': 9.0, 'size': 1.0}, run_id=keeper
    )
    with db.session() as s:
        revision = s.get_one(Candidate, kept).revision
    tell(db, campaign)
    tell(db, campaign)
    assert messages(db, keeper) == []
    assert messages(db, other) == [
        f'[piloop] The head moved to {revision} (candidate#{kept}). Merge {revision} into your work to build on it.'
    ]
    with Repo(campaign.path) as repo:
        repo.git.worktree('add', '--detach', str(tmp_path / 'worktree'), 'HEAD')
    with Repo(tmp_path / 'worktree') as worktree:
        assert worktree.git.cat_file('-t', revision) == 'commit', 'a worktree shares the objects of its repo'
    with Repo(campaign.path) as repo:
        assert repo.git.rev_parse('refs/piloop/demo/head') == revision, 'the pin keeps the head reachable'


def test_a_run_that_started_from_the_head_or_whose_worker_is_not_running_yet_hears_nothing(
    db: Database, campaign: Campaign
) -> None:
    follow(db, campaign)
    kept = keep_elsewhere(db, campaign, 'kept', {'solve.txt': 'base\nkept\n'}, {'score': 9.0, 'size': 1.0})
    fresh = running(db, campaign, 'fresh', base=kept)
    starting = running(db, campaign, 'starting', state=AgentSessionState.STARTING)
    tell(db, campaign)
    assert (messages(db, fresh), messages(db, starting)) == ([], [])


def test_without_follow_head_a_keep_sends_nothing_and_pins_nothing(db: Database, campaign: Campaign) -> None:
    other = running(db, campaign, 'other')
    keep_elsewhere(db, campaign, 'kept', {'solve.txt': 'base\nkept\n'}, {'score': 9.0, 'size': 1.0})
    tell(db, campaign)
    assert messages(db, other) == []
    with Repo(campaign.path) as repo:
        assert 'refs/piloop/demo/head' not in [r.path for r in repo.refs]


def test_the_daemon_tells_the_workers_on_a_verdict(
    db: Database,
    campaign: Campaign,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    follow(db, campaign)
    other = running(db, campaign, 'other')
    keep_elsewhere(db, campaign, 'kept', {'solve.txt': 'base\nkept\n'}, {'score': 9.0, 'size': 1.0})
    [consumer] = [c for c in make_scheduler(db, runtime, tmp_path, tracer).consumers() if c.name == 'follow_head']
    assert consumer.wakes_on == frozenset({VerdictChanged})
    consumer.run(Turn(utcnow()))
    assert len(messages(db, other)) == 1
    assert dict(span_named(spans, 'campaign.head_told').attributes or {}) == {'campaign': 'demo', 'messages': 1}
