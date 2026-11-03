"""Submissions: an implementer's version scored into the pool while it works, and what its worker hears of it."""

import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pytest_mock import MockerFixture
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.campaign.ops import Refused
from piloop.config import CampaignConfig
from piloop.daemon.loop import Turn
from piloop.daemon.main import Daemon
from piloop.db.events import in_order, latest
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.ids import CommentKind, Revision, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import measure
from piloop.plugins.campaign_type.kit.evaluation.jobs import InfraFailed
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Split,
    request_remeasure,
    requested,
    sealed_evaluator,
    sealed_of,
)
from piloop.plugins.campaign_type.kit.evaluation.submissions import (
    SubmitRefused,
    submit,
    submitted as submissions_of,
    tell_scores,
    waiting,
)
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator, Measurement
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import Measuring, RunController, close_run
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import end_run
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.escalation.contract import ESCALATION
from piloop.plugins.vcs.git import Git
from piloop.settings import Settings
from tests.conftest import (
    SETTLE,
    ScriptRuntime,
    baseline,
    factory,
    register,
    reports,
    run_all,
    serving_api,
    span_named,
    wait_until,
    workers_of,
    write_toml,
)
from tests.daemon.test_evaluation_queue import queued_executor
from tests.daemon.test_run_controller import controller, make_run, run_row, scored_elsewhere, set_config
from tests.daemon.test_scheduler import make_scheduler

SUBMIT = {'scoring': 'submit', 'noise_runs': 1}
CONFIRM = {'scoring': 'submit', 'noise_runs': 3}
"""A first score that beats the best's is evaluated until it has 3 evaluations, and scored on the sealed split."""


def worktree(campaign: Campaign, tmp_path: Path) -> Path:
    """An implementer's checkout of the campaign's best."""
    return worktree_of(campaign, tmp_path, 'checkout')


def worktree_of(campaign: Campaign, tmp_path: Path, name: str) -> Path:
    """An implementer's checkout of the campaign's best, called ``name``."""
    path = tmp_path / name
    Repo(campaign.path).git.worktree('add', '--detach', str(path), 'HEAD')
    return path


def commit(checkout: Path, lines: str) -> Revision:
    """Write ``lines`` to ``solve.txt`` and commit: the score of the tree is its number of lines."""
    (checkout / 'solve.txt').write_text(lines)
    repo = Repo(checkout)
    repo.git.commit('-qam', f'{lines.count(chr(10))} lines')
    return Revision(repo.head.commit.hexsha)


def running(db: Database, campaign: Campaign, title: str = 'more lines') -> RunId:
    run_id = make_run(db, campaign, title=title)
    with db.session() as s:
        s.get_one(Run, run_id).status = RunStatus.RUNNING
        s.commit()
    return run_id


def submitted(db: Database, campaign: Campaign, run_id: RunId, revision: Revision) -> CandidateId:
    with db.session() as s, closing(Git().open(Path(campaign.path))) as repo:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        candidate = submit(
            s, s.get_one(Run, run_id), repo.version(revision), repo, Agent(role='implementer', session=agent.id)
        )
        s.commit()
        return candidate.id


def scored(db: Database, candidate_id: CandidateId) -> CandidateScored | None:
    """The last ``candidate.scored`` of the candidate, or None."""
    with db.session() as s:
        found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate_id))
        return found[1] if found is not None else None


def best(db: Database, campaign: Campaign) -> CandidateId | None:
    with db.session() as s:
        found = best_of(s, s.get_one(Campaign, campaign.id))
        return found.id if found is not None else None


def waiting_of(db: Database, campaign: Campaign) -> list[Candidate]:
    with db.session() as s:
        return waiting(s, s.get_one(Campaign, campaign.id))


def waits(db: Database, campaign: Campaign, candidate_id: CandidateId) -> bool:
    """Whether the submission waits for its score, with none recorded."""
    return [c.id for c in waiting_of(db, campaign)] == [candidate_id] and scored(db, candidate_id) is None


@pytest.fixture
def ex(db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path) -> RunController:
    set_config(db, campaign, evaluator=SUBMIT)
    return controller(db, runtime, tmp_path)


def test_a_submission_that_beats_the_best_becomes_it_and_one_that_does_not_is_scored_beside_it(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    better = submitted(db, campaign, run_id, commit(checkout, 'base\na\nb\n'))
    assert waits(db, campaign, better), 'it waits for its score'
    ex.score_submission(better)
    first = scored(db, better)
    assert first is not None and (first.best, first.parent_delta, first.best_delta) == (better, 2.0, None)
    worse = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    ex.score_submission(worse)
    second = scored(db, worse)
    assert second is not None and (second.best, second.parent_delta, second.best_delta) == (better, -1.0, -1.0)
    assert best(db, campaign) == better
    assert waiting_of(db, campaign) == []
    with db.session() as s:
        assert s.get_one(Candidate, worse).parent_id == better, 'a submission builds on the last one'


def test_a_run_submits_without_a_cap_unless_the_campaign_sets_one(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    made = [submitted(db, campaign, run_id, commit(checkout, f'base\n{n}\n')) for n in range(5)]
    assert len(set(made)) == 5, 'no cap by default'


def test_a_run_submits_at_most_its_cap_and_never_a_known_tree(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'submissions': 3})
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    first = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    ex.score_submission(first)
    commit(checkout, 'base\nb\n')
    again = commit(checkout, 'base\na\n')
    assert submitted(db, campaign, run_id, again) == first, 'its own submission again: no submission spent'
    with pytest.raises(SubmitRefused, match=r'its tree is candidate#1 already \(baseline\)'):
        submitted(db, campaign, run_id, commit(checkout, 'base\n'))
    submitted(db, campaign, run_id, commit(checkout, 'base\nc\n'))
    submitted(db, campaign, run_id, commit(checkout, 'base\nd\n'))
    with pytest.raises(SubmitRefused, match=rf'run#{run_id} used its 3 submissions'):
        submitted(db, campaign, run_id, commit(checkout, 'base\ne\n'))


def test_a_run_that_claimed_no_idea_submits_nothing(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """Scored on no idea's track, its version would stand for nothing on the board."""
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    with db.session() as s:
        s.get_one(Run, run_id).idea_id = None
        s.commit()
    with pytest.raises(SubmitRefused, match=rf'run#{run_id} has claimed no idea: `piloop idea claim`'):
        submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))


def test_a_campaign_that_scores_after_the_run_takes_no_submission(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    set_config(db, campaign, evaluator={'scoring': 'run_end'})
    with pytest.raises(SubmitRefused, match='scores every commit after its run ends'):
        submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    set_config(db, campaign, evaluator=SUBMIT)
    with db.session() as s:
        s.get_one(Run, run_id).status = RunStatus.QUEUED
        s.commit()
    with pytest.raises(SubmitRefused, match=rf'run#{run_id} is queued, not running'):
        submitted(db, campaign, run_id, commit(checkout, 'base\nb\n'))


def test_at_the_end_of_a_run_its_versions_that_it_did_not_submit_are_skipped_and_its_submission_settles_its_idea(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """The newest version is skipped, with no score: the run's idea is done on its submission's."""
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    sent = commit(checkout, 'base\na\n')
    ex.score_submission(submitted(db, campaign, run_id, sent))
    later = commit(checkout, 'base\na\nb\n')
    with closing(Git().open(Path(campaign.path))) as repo:
        versions = [repo.version(sent), repo.version(later)]
        last = ex._score(run_id, campaign.id, repo, versions, tmp_path / 'logs')
    with db.session() as s:
        [one, two] = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        assert (one.origin, two.origin, two.measurements) == (Origin.RUN, Origin.SKIPPED, [])
        assert last == one.id
        close_run(s, s.get_one(Run, run_id), RunEnd.FINISHED, None).scoring = True
        s.commit()
    assert scored(db, one.id) is not None and scored(db, two.id) is None
    ex._settle(run_id, RunEnd.FINISHED, last)
    with db.session() as s:
        idea = s.get_one(Run, run_id).idea
        assert idea is not None and idea.status is IdeaStatus.DONE


def queue_of(tmp_path: Path, *scores: int) -> list[str]:
    """An evaluate command that reports the next score of ``scores`` on each run, as a noisy timing would."""
    (tmp_path / 'scores').write_text(''.join(f'{v}\n' for v in scores))
    # POSIX, not GNU: BSD sed takes the argument of -i as a backup suffix
    take = f'v=$(head -1 {tmp_path}/scores); tail -n +2 {tmp_path}/scores > {tmp_path}/rest; mv {tmp_path}/rest {tmp_path}/scores'
    return ['sh', '-c', f'{take}; {reports(score="$v", size="1")}']


SEALED_AGREES = ['sh', '-c', reports(score='$(wc -l < solve.txt)', size='1')]
"""A sealed command that scores as the visible split does."""

HARNESS_SUBMITS = '#!/bin/sh\necho a >> solve.txt\ngit commit -qam more\n"$PILOOP_BIN" submit > ../submitted.txt 2>&1\n"$PILOOP_BIN" run end submitted\n'
"""Commits one line, submits it, keeps what ``piloop submit`` printed in its log directory, and ends its run."""


def test_an_implementer_submits_through_the_daemon_and_works_on_while_it_is_scored(
    campaign_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(campaign_path, HARNESS_SUBMITS)
    home = Settings(home=tmp_path / 'home')
    daemon = Daemon(home)
    try:
        workers_of(daemon.controller).worker.runtime_factory = factory(ScriptRuntime())
        campaign = register(daemon.db, campaign_path)
        set_config(daemon.db, campaign, evaluator=SUBMIT)
        run_id = make_run(daemon.db, campaign)
        with serving_api(home):
            daemon.scheduler.reap()
            run_all(daemon.scheduler)
            wait_until(lambda: waiting_of(daemon.db, campaign), 'the submission')
            score, go, taken = daemon.controller.score_submission, threading.Event(), []

            def held(candidate_id: CandidateId) -> None:
                taken.append(candidate_id)
                go.wait(30)
                score(candidate_id)

            monkeypatch.setattr(daemon.controller, 'score_submission', held)
            run_all(daemon.scheduler)  # what its event wakes in the daemon's loop
            run_all(daemon.scheduler)  # while it is scored: not scored twice
            go.set()
            wait_until(lambda: run_row(daemon.db, run_id).status is RunStatus.SUCCEEDED, 'the run', timeout=120)
            daemon.scheduler.wait(SETTLE)
        with daemon.db.session() as s:
            [made] = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).all()
            assert made.origin is Origin.RUN
            assert taken == [made.id], 'once'
        assert scored(daemon.db, made.id) is not None, 'scored while the run worked'
    finally:
        daemon.shutdown(timeout=5)
    queued = f'candidate#{made.id} is queued: where it stands comes as a [piloop] message\n'
    assert (home.home / 'runs' / str(run_id) / 'submitted.txt').read_text() == queued, 'it returns before its score'


def test_a_submission_whose_evaluation_halts_waits_for_its_score(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    monkeypatch.setattr(ex, '_measure', lambda *args, **kwargs: Measuring.STOPPED)
    ex.score_submission(candidate)
    assert waits(db, campaign, candidate), 'the next daemon scores it'


def test_a_submission_that_piloop_fails_to_score_waits_for_its_score(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))

    def broken(*args: object, **kwargs: object) -> Measuring:
        raise RuntimeError('the repo is gone')

    monkeypatch.setattr(ex, '_measure', broken)
    ex.score_submission(candidate)
    assert waits(db, campaign, candidate), 'a fault of piloop is no score: the next pass scores it again'


def first_only[**P](fn: Callable[P, Measuring], calls: list[object]) -> Callable[P, Measuring]:
    """``fn`` for its first call; every later one is cut by a stop."""

    def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
        calls.append(args)
        return fn(*args, **kwargs) if len(calls) == 1 else Measuring.STOPPED

    return measured


def test_a_confirmation_that_a_stop_ends_leaves_the_submission_waiting(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline scores 10, 12, 14. The submission's 15 beats their mean: it is evaluated again, and a stop cuts it."""
    set_config(db, campaign, evaluate=queue_of(tmp_path, 10, 12, 14, 15), evaluator=CONFIRM)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    calls: list[object] = []
    monkeypatch.setattr(ex, '_measure', first_only(ex._measure, calls))
    ex.score_submission(candidate)
    assert len(calls) == 2, 'its first evaluation, then the confirmation'
    assert waits(db, campaign, candidate)


def test_the_sealed_evaluator_that_another_session_inserted_first_is_the_one_both_use(
    db: Database, campaign: Campaign, mocker: MockerFixture
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED_AGREES})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        first = sealed_evaluator(s, p, p.config)
        s.commit()
    looked: list[bool] = []

    def before_the_insert(s: Session, p: Campaign, cfg: CampaignConfig) -> Evaluator | None:
        """It looks before the other session's insert, then again after its own insert conflicts."""
        looked.append(True)
        return None if len(looked) == 1 else sealed_of(s, p, cfg)

    mocker.patch.object(measure, 'sealed_of', side_effect=before_the_insert)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        raced = sealed_evaluator(s, p, p.config)
        assert first is not None and raced is not None and raced.id == first.id
    assert len(looked) == 2


def sealed_stops[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
    """``fn``, but a stop cuts each evaluation on the sealed split."""

    def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
        return Measuring.STOPPED if kwargs.get('split') is Split.SEALED else fn(*args, **kwargs)

    return measured


def test_a_sealed_score_that_a_stop_ends_leaves_the_submission_waiting(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_config(db, campaign, evaluator={**CONFIRM, 'sealed': SEALED_AGREES})
    ex = controller(db, runtime, tmp_path)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    monkeypatch.setattr(ex, '_measure', sealed_stops(ex._measure))
    ex.score_submission(candidate)
    assert waits(db, campaign, candidate)


def scoring(db: Database, run_id: RunId) -> bool:
    with db.session() as s:
        return s.get_one(Run, run_id).scoring


def test_a_run_that_ends_while_its_submission_is_scored_settles_on_its_score(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """However it ends, a lost worker or no version at all, its idea waits for the score; a stop leaves it scoring."""
    run_id = running(db, campaign)
    candidate = submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n'))
    with db.session() as s:
        close_run(s, s.get_one(Run, run_id), RunEnd.WORKER_LOST, 'lost').scoring = True
        s.commit()
    settling = threading.Thread(target=ex._settle, args=(run_id, RunEnd.WORKER_LOST, None))
    settling.start()
    settling.join(0.5)
    assert settling.is_alive() and scoring(db, run_id), 'it waits for the score'
    ex.score_submission(candidate)
    settling.join(30)
    assert not scoring(db, run_id)
    other = running(db, campaign, 'other')
    submitted(db, campaign, other, commit(worktree_of(campaign, tmp_path, 'second'), 'base\nb\n'))
    ex.launcher.cancel()
    ex._settle(other, RunEnd.FINISHED, None)
    with db.session() as s:
        assert s.get_one(Run, other).status is RunStatus.RUNNING, 'a stop settles nothing: the next daemon does'


def test_the_gain_over_the_parent_and_over_the_best_are_recorded_apart(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """The best moved past the run's base: the submission beats its parent, not the best, and is recorded so."""
    run_id = running(db, campaign)
    elsewhere = scored_elsewhere(db, campaign, 'elsewhere', {'other.txt': 'x\n'}, {'score': 3.0, 'size': 1.0})
    candidate = submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(candidate)
    found = scored(db, candidate)
    assert found is not None and (found.best, found.parent_delta, found.best_delta) == (elsewhere, 1.0, -1.0)


BAD_SEALED = [
    'sh',
    '-c',
    'printf \'%s\\n\' \'{"status": "ok", "score": 1, "direction": "lower"}\' >> "$PILOOP_RESULT_FILE"',
]
"""A sealed command whose result piloop refuses: the campaign's primary, score, is higher-is-better."""


def test_a_sealed_result_that_piloop_refuses_is_no_score_and_escalates_once_until_the_command_is_fixed(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A candidate cannot change the sealed command: its bad result is the setup's fault, never the candidate's. The
    submission waits while its sealed evaluation runs again, one escalation says why, and a fixed command scores it."""
    set_config(db, campaign, evaluator={**CONFIRM, 'sealed': BAD_SEALED})
    ex = queued_executor(db, runtime, tmp_path, 1)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    scoring_it = threading.Thread(target=ex.score_submission, args=(candidate,))
    scoring_it.start()
    wait_until(lambda: len(failures(db)) >= 2, 'the sealed evaluation failed twice, and waits to run again')
    assert waits(db, campaign, candidate), 'no score'
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None
        assert s.scalars(select(Measurement).where(Measurement.evaluator_id == hidden.id)).all() == []
        [escalation] = s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all()
        assert 'it reports the direction lower; [[metrics]] says score is higher' in escalation.body
    set_config(db, campaign, evaluator={**CONFIRM, 'sealed': SEALED_AGREES})
    scoring_it.join(30)
    assert not scoring_it.is_alive(), 'a new sealed command ends the wait for the old one'
    ex.score_submission(candidate)
    assert best(db, campaign) == candidate, 'confirmed, and it beats the baseline on the sealed split'


def failures(db: Database) -> list[InfraFailed]:
    with db.session() as s:
        return [e for _, e in in_order(s, InfraFailed)]


def test_a_sealed_split_that_reports_an_infra_error_gives_no_score_and_scores_it_once_it_answers(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An evaluator that says its fault is the machine's (``infra_error``), a GPU out of memory say, is believed on the
    sealed split too: the submission waits, and is scored there once the split answers. A ``crash`` is the
    candidate's: only the evaluator can tell the two apart."""
    oom = tmp_path / 'oom'
    oom.touch()
    infra = '{"status": "infra_error", "feedback": "CUDA out of memory"}'
    flaky = [
        'sh',
        '-c',
        f'if [ -f {oom} ]; then printf "%s\\n" \'{infra}\' >> "$PILOOP_RESULT_FILE"; exit 0; fi; '
        + reports(score='$(wc -l < solve.txt)', size='1'),
    ]
    set_config(db, campaign, evaluator={**CONFIRM, 'sealed': flaky})
    ex = queued_executor(db, runtime, tmp_path, 1)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    scoring_it = threading.Thread(target=ex.score_submission, args=(candidate,))
    scoring_it.start()
    wait_until(lambda: len(failures(db)) >= 1, 'the sealed evaluation failed for the machine, and waits to run again')
    assert waits(db, campaign, candidate), 'no score'
    oom.unlink()
    scoring_it.join(30)
    assert best(db, campaign) == candidate, 'scored on the sealed split, where it beats the baseline'


def ending(db: Database, run_id: RunId, how: RunEnding, reason: str | None = None) -> None:
    """The run's implementer runs ``piloop run end``."""
    with db.session() as s:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        agent.state = AgentSessionState.RUNNING
        s.commit()
        end_run(s, how, reason, actor=Agent(role='implementer', session=agent.id))
        s.commit()


def test_a_run_that_submitted_nothing_ends_as_given_up_not_as_submitted(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """Its idea goes back to the board with the reason, instead of done with no result."""
    empty = running(db, campaign)
    with pytest.raises(
        Refused, match=rf'^run#{empty} submitted nothing: `piloop submit` first, or `piloop run end gave_up'
    ):
        ending(db, empty, RunEnding.SUBMITTED)
    ending(db, empty, RunEnding.GAVE_UP, 'the cache idea cannot run here')
    tried = running(db, campaign, 'tried')
    submitted(db, campaign, tried, commit(worktree(campaign, tmp_path), 'base\na\n'))
    ending(db, tried, RunEnding.SUBMITTED)
    set_config(db, campaign, evaluator={'scoring': 'run_end'})
    ending(
        db, running(db, campaign, 'after the run'), RunEnding.SUBMITTED
    )  # every commit is scored after the run: nothing to submit


def agent_running(db: Database, run_id: RunId) -> None:
    with db.session() as s:
        s.scalars(select(AgentSession).where(working_on(run_id))).one().state = AgentSessionState.RUNNING
        s.commit()


def told(db: Database, campaign: Campaign, run_id: RunId) -> list[str]:
    """Tell the scores, twice, and return the messages that the run's worker got."""
    for _ in range(2):
        with db.session() as s:
            tell_scores(s, s.get_one(Campaign, campaign.id), 200)
            s.commit()
    with db.session() as s:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        found = select(Comment.body).where(
            Comment.target == TargetRef(TargetKind.AGENT_SESSION, agent.id), Comment.kind == CommentKind.MESSAGE
        )
        return list(s.scalars(found.order_by(Comment.id)))


def test_the_worker_hears_each_score_of_its_submissions_once_as_a_message(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """``piloop submit`` returns at once: where the version stands reaches the worker in its turn, once per score."""
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    agent_running(db, run_id)
    revision = commit(checkout, 'base\na\n')
    candidate = submitted(db, campaign, run_id, revision)
    assert told(db, campaign, run_id) == [], 'no score yet'
    ex.score_submission(candidate)
    assert told(db, campaign, run_id) == [
        f'[piloop] candidate#{candidate} ({revision[:12]}): score 2 over 1 eval: the best of the score track now,'
        ' rank 1 of 2; vs parent +1'
    ]
    with db.session() as s:
        request_remeasure(s, s.get_one(Candidate, candidate), System.HUMAN, repeats=1)
        s.commit()
    ex.remeasure(candidate)
    assert len(told(db, campaign, run_id)) == 2, 'scored again: told again'


def test_a_long_score_line_is_cut_to_the_steer_line_chars(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    agent_running(db, run_id)
    revision = commit(checkout, 'base\na\n')
    candidate = submitted(db, campaign, run_id, revision)
    ex.score_submission(candidate)
    with db.session() as s:
        tell_scores(s, s.get_one(Campaign, campaign.id), 10)
        s.commit()
        [message] = s.scalars(select(Comment.body).where(Comment.kind == CommentKind.MESSAGE)).all()
    assert message == f'[piloop] candidate#{candidate} ({revision[:12]}): score 2 o…'


def test_a_worker_that_is_not_running_hears_nothing(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id = running(db, campaign)
    candidate = submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(candidate)
    assert told(db, campaign, run_id) == [], 'its worker is not running'


FLOORED = [
    {'name': 'score', 'direction': 'higher', 'role': 'primary'},
    {'name': 'size', 'direction': 'lower', 'role': 'guardrail', 'margin': 100, 'floor': 8},
]
"""A guardrail with a floor, as a CPU-time limit has: a version of more than 8 bytes is past it."""
TIMES_OUT = [
    'sh',
    '-c',
    'grep -q slow solve.txt && { echo \'{"status": "timeout"}\' >> "$PILOOP_RESULT_FILE"; exit 0; }; '
    + reports(score='$(wc -l < solve.txt)', size='$(wc -c < solve.txt)'),
]
"""An evaluate command that runs out of time on a version that says ``slow``."""


@pytest.mark.parametrize(
    ('lines', 'again'),
    [('base\nabcdefgh\n', False), ('base\nslow\n', True), ('other\n', False)],
    ids=['past-a-floor', 'timed-out', 'a-regression'],
)
def test_a_resubmit_of_a_tree_whose_every_evaluation_failed_measures_it_again_and_any_other_answers_as_it_is(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, lines: str, again: bool
) -> None:
    """Load can push a version past the timeout: a resubmit of a tree that never measured asks one more evaluation. A
    version that scored keeps its score, past a floor or not, and another run's submission of the tree is refused."""
    set_config(db, campaign, evaluate=TIMES_OUT, evaluator=SUBMIT, metrics=FLOORED)
    ex = controller(db, runtime, tmp_path)
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    agent_running(db, run_id)
    revision = commit(checkout, lines)
    candidate = submitted(db, campaign, run_id, revision)
    ex.score_submission(candidate)
    assert len(told(db, campaign, run_id)) == 1
    assert submitted(db, campaign, run_id, revision) == candidate
    assert submitted(db, campaign, run_id, revision) == candidate
    with db.session() as s:
        assert len(requested(s, s.get_one(Campaign, campaign.id))) == int(again), 'asked once, while it waits'
        assert submissions_of(s, run_id) == [candidate], 'no submission spent'
    with pytest.raises(SubmitRefused, match=rf'its tree is candidate#{candidate} already \(run\)'):
        submitted(db, campaign, running(db, campaign, 'other'), revision)
    if again:
        ex.remeasure(candidate)
        with db.session() as s:
            assert len(s.get_one(Candidate, candidate).measurements) == 2, 'measured again'
        assert len(told(db, campaign, run_id)) == 2, 'it hears where it stands now'


def test_a_submission_is_measured_on_both_splits_at_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed split does not wait for the confirmation: the score comes in the time of the slower of the two."""
    set_config(db, campaign, evaluator={**CONFIRM, 'sealed': SEALED_AGREES})
    ex = queued_executor(db, runtime, tmp_path, 2)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    sealing, overlapped, sealed_runs, visible_runs = threading.Event(), [], [], []

    def both[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
        def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
            if kwargs.get('split') is Split.SEALED:
                sealed_runs.append(args[4])
                sealing.set()
            elif args[4] == candidate:
                visible_runs.append(kwargs.get('repeats', 1))
                if len(visible_runs) == 2:  # the confirmation
                    overlapped.append(sealing.wait(10))
            return fn(*args, **kwargs)

        return measured

    monkeypatch.setattr(ex, '_measure', both(ex._measure))
    ex.score_submission(candidate)
    assert visible_runs == [1, 2], 'its first evaluation, then the two that confirm it'
    assert overlapped == [True], 'the sealed split started while the confirmation ran'
    assert sorted(sealed_runs) == [1, candidate], 'the submission and the baseline that it must beat there'
    assert best(db, campaign) == candidate
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None
        sealed = select(Measurement).where(Measurement.evaluator_id == hidden.id, Measurement.candidate_id == candidate)
        assert len(s.scalars(sealed).all()) == 1, 'scored once on the sealed split'


def test_the_daemon_tells_a_worker_where_its_submission_stands_on_a_score(
    db: Database,
    campaign: Campaign,
    ex: RunController,
    runtime: ScriptRuntime,
    tmp_path: Path,
    tracer: Tracer,
    spans: InMemorySpanExporter,
) -> None:
    run_id = running(db, campaign)
    agent_running(db, run_id)
    ex.score_submission(submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n')))
    [consumer] = [c for c in make_scheduler(db, runtime, tmp_path, tracer).consumers() if c.name == 'scores']
    assert consumer.wakes_on == frozenset({CandidateScored})
    consumer.run(Turn(utcnow()))
    assert dict(span_named(spans, 'campaign.scores_told').attributes or {}) == {'campaign': 'demo', 'messages': 1}
