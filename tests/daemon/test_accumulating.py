"""The ``accumulating`` campaign type beyond what its model's replay takes: the trunk's guard, the type and the planner.

``test_accumulating_model.py`` replays the model's graphs. These take what the graphs do not reach: a merge whose trunk
moved, one that adds nothing or scores worse than the trunk, the merge job's thread, and the type's own hooks.
"""

from collections.abc import Callable
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from sqlalchemy import select

from piloop.actor import System
from piloop.campaign.campaigns import sync
from piloop.config import ConfigError
from piloop.db.events import emit, in_order
from piloop.db.models import Campaign, Comment
from piloop.db.session import Database
from piloop.ids import CommentKind, Revision, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.accumulating import Accumulating
from piloop.plugins.campaign_type.accumulating.planner import AccumulatingPlanner
from piloop.plugins.campaign_type.accumulating.trunk import Batch, TrunkAdvanced, decide, head, start
from piloop.plugins.campaign_type.contract import TypeHost
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import submissions
from piloop.plugins.campaign_type.kit.evaluation.submissions import tell_scores
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import Measuring, close_run
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunId
from piloop.plugins.campaign_type.kit.runs.stop import settle_claims
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.vcs.git import Git
from piloop.plugins.vcs.git.commands import commit_env
from piloop.settings import AgentSettings
from tests.conftest import ScriptRuntime
from tests.daemon.test_accumulating_model import WORKER, fresh
from tests.daemon.test_pool import controller, implementer, running, submitted
from tests.daemon.test_submissions import agent_running

STEER_LINE_CHARS = AgentSettings().steer_line_chars


def work(campaign: Campaign, tmp_path: Path, name: str, files: dict[str, str]) -> Revision:
    """A commit of ``files`` from the baseline, in a worktree of its own: a run's work."""
    path = tmp_path / name
    Repo(campaign.path).git.worktree('add', '-q', '--detach', str(path))
    for file, text in files.items():
        (path / file).write_text(text)
    repo = Repo(path)
    repo.git.add('-A')
    repo.git.commit('-qm', name, env=commit_env(WORKER, WORKER))
    return Revision(repo.head.commit.hexsha)


def batch_of(db: Database, campaign: Campaign) -> Batch | None:
    with db.session() as s, closing(Git().open(Path(campaign.path))) as repo:
        found = start(s, repo, s.get_one(Campaign, campaign.id))
        s.commit()
    return found


def scored(db: Database, candidate: CandidateId, value: float) -> None:
    """Record a measurement of ``candidate`` under its campaign's evaluator, as the evaluate command would."""
    with db.session() as s:
        evaluator = s.get_one(Campaign, s.get_one(Candidate, candidate).campaign_id).evaluator_id
        s.add(Measurement(candidate_id=candidate, evaluator_id=evaluator, metrics={'score': value, 'size': 1.0}))
        s.commit()


def told(db: Database, candidate: CandidateId) -> str:
    with db.session() as s:
        return [e for e in in_order(s, CandidateScored) if e[0].target_id == candidate][-1][1].told


def test_a_merge_that_started_on_an_older_trunk_is_not_taken_and_its_submissions_wait(
    db: Database, tmp_path: Path
) -> None:
    campaign = fresh(db, tmp_path, 'moved')
    mine = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'mine', {'a.txt': 'a\n'}))
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (mine,)
    scored(db, batch.merge, 3)
    with db.session() as s:
        other = Candidate(campaign_id=campaign.id, tree_sha='other', revision='other', origin=Origin.MERGE)
        s.add(other)
        s.flush()
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None
        advanced = TrunkAdvanced(candidate=other.id, base=trunk.id, merged=())
        emit(s, advanced, TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
        s.commit()
    with db.session() as s:
        assert decide(s, batch) is False, 'the trunk moved since the merge started'
        s.commit()
    with db.session() as s:
        assert [c.id for c in submissions.waiting(s, s.get_one(Campaign, campaign.id))] == [mine], 'it waits'


def test_a_merge_that_adds_nothing_or_scores_worse_than_the_trunk_is_refused_with_why(
    db: Database, tmp_path: Path
) -> None:
    campaign = fresh(db, tmp_path, 'worse')
    first = submitted(
        db, campaign, running(db, campaign), work(campaign, tmp_path, 'first', {'a.txt': 'a\n', 'c.txt': 'c\n'})
    )
    batch = batch_of(db, campaign)
    assert batch is not None
    scored(db, batch.merge, 2)
    with db.session() as s:
        assert decide(s, batch) is True
        s.commit()
    assert told(db, first) == f'merged into the trunk: candidate#{batch.merge}, score 2'
    # its own file, which the trunk has: the merge is the trunk again
    same = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'same', {'a.txt': 'a\n'}))
    assert batch_of(db, campaign) is None
    assert told(db, same).endswith('. Why: the trunk has its work already')
    worse = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'worse-run', {'b.txt': 'b\n'}))
    batch = batch_of(db, campaign)
    assert batch is not None
    scored(db, batch.merge, 1)
    with db.session() as s:
        assert decide(s, batch) is False
        s.commit()
    assert told(db, worse).endswith(
        f'. Why: the merge onto the trunk (candidate#{batch.merge}) has score 1, worse than the trunk 2'
    )


def test_a_conflict_refusal_tells_its_worker_the_trunk_to_merge_also_when_the_line_is_cut(
    db: Database, tmp_path: Path
) -> None:
    """Git's output on many files is longer than a steer line: what to do comes first, and only git's output is cut."""
    campaign = fresh(db, tmp_path, 'cut')
    files = {f'part{n}.py': f'first {n}\n' for n in range(6)}
    first = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'first', files))
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (first,)
    scored(db, batch.merge, 2)
    with db.session() as s:
        assert decide(s, batch) is True
        s.commit()
    run_id = running(db, campaign)
    agent_running(db, run_id)
    clashing = submitted(db, campaign, run_id, work(campaign, tmp_path, 'second', {f: f'second {f}\n' for f in files}))
    assert batch_of(db, campaign) is None, 'it conflicts: refused before any merge'
    assert len(told(db, clashing)) > STEER_LINE_CHARS, 'git said more than a steer line holds'
    with db.session() as s:
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None
        tell_scores(s, s.get_one(Campaign, campaign.id), STEER_LINE_CHARS)
        s.commit()
        [message] = s.scalars(select(Comment.body).where(Comment.kind == CommentKind.MESSAGE)).all()
    assert f'merge the trunk ({trunk.revision}) into your work, then submit again' in message
    assert message.endswith('…'), "cut: only the end of git's output is gone"


def test_a_version_that_merged_the_trunk_and_kept_its_own_files_is_merged_again_though_its_tree_was_refused(
    db: Database, tmp_path: Path
) -> None:
    """The issue's worker: its merge of the trunk has the tree of its refused submission, and a history that merges.

    The same version onto the same trunk is the same merge: a retry, answered as it is.
    """
    campaign = fresh(db, tmp_path, 'kept')
    first = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'first', {'fib.py': 'first\n'}))
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (first,)
    scored(db, batch.merge, 1)
    with db.session() as s:
        assert decide(s, batch) is True
        s.commit()
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None
    run_id = running(db, campaign)
    agent_running(db, run_id)
    refused = work(campaign, tmp_path, 'mine', {'fib.py': 'mine\n'})
    mine = submitted(db, campaign, run_id, refused)
    assert batch_of(db, campaign) is None, 'it conflicts with the trunk'
    assert submitted(db, campaign, run_id, refused) == mine, 'a retry'
    worktree = Repo(tmp_path / 'mine')
    worktree.git.merge('-q', '-s', 'ours', '--no-edit', trunk.revision, env=commit_env(WORKER, WORKER))
    merged_in = Revision(worktree.head.commit.hexsha)
    assert worktree.git.rev_parse(f'{merged_in}^{{tree}}') == worktree.git.rev_parse(f'{refused}^{{tree}}')
    assert submitted(db, campaign, run_id, merged_in) == mine, 'the same tree: the same candidate'
    assert submitted(db, campaign, run_id, merged_in) == mine, 'a retry of the new submission'
    with db.session() as s:
        assert [c.id for c in submissions.waiting(s, s.get_one(Campaign, campaign.id))] == [mine], 'it waits again'
        assert submissions.submitted(s, run_id) == [mine, mine], 'two submissions'
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (mine,), 'its history holds the trunk: it merges'
    scored(db, batch.merge, 1)
    with db.session() as s:
        assert decide(s, batch) is True
        s.commit()
        now = head(s, s.get_one(Campaign, campaign.id))
        assert now is not None and now.revision == merged_in, 'the trunk keeps the history that it merged'
        tell_scores(s, s.get_one(Campaign, campaign.id), STEER_LINE_CHARS)
        s.commit()
        [message] = s.scalars(select(Comment.body).where(Comment.kind == CommentKind.MESSAGE)).all()
    assert message.startswith(f'[piloop] candidate#{mine} ({merged_in[:12]}): merged into the trunk'), message


def test_a_tree_submitted_again_for_another_claim_settles_both_claims_on_its_merge(
    db: Database, tmp_path: Path
) -> None:
    """A run submits a tree for idea A, claims B, merges the trunk into its work and keeps its files: the same tree,
    submitted again for B. Each submission keeps the claim that it was for, so the merge makes both ideas done."""
    campaign = fresh(db, tmp_path, 'two-claims')
    merges(db, campaign, running(db, campaign), work(campaign, tmp_path, 'first', {'fib.py': 'first\n'}))
    with db.session() as s:
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None
    run_id = running(db, campaign)
    agent_running(db, run_id)
    refused = work(campaign, tmp_path, 'mine', {'fib.py': 'mine\n'})
    mine = submitted(db, campaign, run_id, refused)
    assert batch_of(db, campaign) is None, 'it conflicts with the trunk'
    agent = implementer(db, run_id)
    with db.session() as s:
        other = Idea(campaign_id=campaign.id, track='score', title='b', hypothesis='h', created_by='test')
        s.add(other)
        s.flush()
        backlog_ops.claim(s, other.id, actor=agent)
        s.commit()
        second = other.id
    worktree = Repo(tmp_path / 'mine')
    worktree.git.merge('-q', '-s', 'ours', '--no-edit', trunk.revision, env=commit_env(WORKER, WORKER))
    assert submitted(db, campaign, run_id, Revision(worktree.head.commit.hexsha)) == mine, 'the same tree, for B'
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (mine,)
    scored(db, batch.merge, 1)
    with db.session() as s:
        assert decide(s, batch) is True
        run = s.get_one(Run, run_id)
        settle_claims(s, close_run(s, run, RunEnd.FINISHED, None), RunEnd.FINISHED)
        first = run.claims[0].idea_id
        assert {i: s.get_one(Idea, i).status for i in (first, second)} == {
            first: IdeaStatus.DONE,
            second: IdeaStatus.DONE,
        }, 'each claim settles on its own submissions'


def merges(db: Database, campaign: Campaign, run_id: RunId, revision: Revision) -> CandidateId:
    """Submit ``revision`` of ``run_id`` and merge it into the trunk alone: the trunk moves."""
    mine = submitted(db, campaign, run_id, revision)
    batch = batch_of(db, campaign)
    assert batch is not None and batch.members == (mine,)
    scored(db, batch.merge, 1)
    with db.session() as s:
        assert decide(s, batch) is True
        s.commit()
    return mine


def test_the_same_version_after_the_trunk_moved_is_merged_again_unless_the_trunk_holds_it(
    db: Database, tmp_path: Path
) -> None:
    campaign = fresh(db, tmp_path, 'moved-on')
    merges(db, campaign, running(db, campaign), work(campaign, tmp_path, 'first', {'fib.py': 'first\n'}))
    clashing_run = running(db, campaign)
    clashing = work(campaign, tmp_path, 'clashing', {'fib.py': 'mine\n'})
    refused = submitted(db, campaign, clashing_run, clashing)
    assert batch_of(db, campaign) is None, 'it conflicts with the trunk'
    other_run = running(db, campaign)
    other = merges(db, campaign, other_run, work(campaign, tmp_path, 'other', {'other.py': 'other\n'}))
    assert submitted(db, campaign, clashing_run, clashing) == refused
    with db.session() as s:
        assert submissions.submitted(s, clashing_run) == [refused, refused], 'refused on another trunk: merged again'
    assert batch_of(db, campaign) is None, 'it still conflicts'
    merges(db, campaign, running(db, campaign), work(campaign, tmp_path, 'last', {'last.py': 'last\n'}))
    with db.session() as s:
        revision = s.get_one(Candidate, other).revision
    assert submitted(db, campaign, other_run, revision) == other
    with db.session() as s:
        assert submissions.submitted(s, other_run) == [other], 'merged: the trunk holds it, whatever trunk is now'


def test_the_type_starts_work_from_the_trunk_takes_one_track_and_settles_a_cancel_or_a_settled_idea(
    db: Database, tmp_path: Path
) -> None:
    campaign = fresh(db, tmp_path, 'type')
    run_id = running(db, campaign)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        assert Accumulating().base(s, row) == head(s, row)
        run = s.get_one(Run, run_id)
        settle_claims(s, run, RunEnd.CANCELLED)
        assert s.get_one(Idea, run.claims[0].idea_id).status is IdeaStatus.OPEN, 'a cancel opens it again'
        settle_claims(s, run, RunEnd.FINISHED)
        assert s.get_one(Idea, run.claims[0].idea_id).status is IdeaStatus.OPEN, (
            'an idea that is not claimed stays as it is'
        )
        s.rollback()
    with db.session() as s:
        assert sync(s, s.get_one(Campaign, campaign.id)) is False, 'one track: the config stands'
    toml = Path(campaign.path) / 'piloop.toml'
    toml.write_text(
        toml.read_text().replace(
            '[[metrics]]\nname = "score"\ndirection = "higher"\nrole = "primary"\n',
            '[[metrics]]\nname = "score"\ndirection = "higher"\nrole = "primary"\nmargin = 1\n\n'
            '[[metrics]]\nname = "size"\ndirection = "lower"\nrole = "primary"\nmargin = 1\n',
        )
    )
    with db.session() as s, pytest.raises(ConfigError, match='an accumulating campaign has one track'):
        sync(s, s.get_one(Campaign, campaign.id))


class Measured:
    """A controller's ``measure_once`` that answers what a test says, and records what it measured."""

    def __init__(self, measure: Callable[[CandidateId], Measuring], answers: list[Measuring | Exception]) -> None:
        self.measure, self.answers = measure, answers

    def __call__(self, candidate: CandidateId) -> Measuring:
        answer = self.answers.pop(0) if self.answers else Measuring.MEASURED
        if isinstance(answer, Exception):
            raise answer
        if answer is Measuring.MEASURED:
            return self.measure(candidate)
        return answer


def test_the_merge_job_measures_the_trunk_then_the_merge_decides_it_and_never_raises(
    db: Database, tmp_path: Path, runtime: ScriptRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = fresh(db, tmp_path, 'job')
    ex = controller(db, runtime, tmp_path)
    mine = submitted(
        db, campaign, running(db, campaign), work(campaign, tmp_path, 'mine', {'a.txt': 'a\n', 'c.txt': 'c\n'})
    )
    measured = Measured(
        ex.measure_once, [Measuring.STOPPED, Measuring.MEASURED, Measuring.CANCELLED, RuntimeError('boom')]
    )
    monkeypatch.setattr(ex, 'measure_once', measured)

    bodies: list[tuple[Callable[[], None], Future[None]]] = []

    def submit(body: Callable[[], None]) -> Future[None]:  # the daemon's thread, run once the pass ends
        bodies.append((body, Future()))
        return bodies[-1][1]

    planner = AccumulatingPlanner(TypeHost(Tracer(None), tmp_path, ex, submit), Accumulating())
    for _ in range(4):
        with db.session() as s:
            planner.remeasure(s, s.get_one(Campaign, campaign.id))
        assert set(planner.in_flight()) >= {f'trunk#{campaign.id}'}
        while bodies:
            body, done = bodies.pop(0)
            body()
            done.set_result(None)
    assert planner.in_flight() == {}
    with db.session() as s:
        [(_, advanced)] = in_order(s, TrunkAdvanced)
    assert advanced.merged == (mine,), 'a stop, a cancel and an error left it waiting; the fourth pass merged it'
    held = submitted(db, campaign, running(db, campaign), work(campaign, tmp_path, 'held', {'a.txt': 'a\n'}))
    for _ in range(2):  # the trunk has its work: refused, with no merge to measure; then nothing waits
        with db.session() as s:
            planner.remeasure(s, s.get_one(Campaign, campaign.id))
        while bodies:
            body, done = bodies.pop(0)
            body()
            done.set_result(None)
    assert told(db, held).endswith('. Why: the trunk has its work already')
    assert batch_of(db, campaign) is None, 'nothing waits'
