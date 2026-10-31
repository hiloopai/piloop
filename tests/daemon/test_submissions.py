"""Submissions: an implementer's version scored while it works, against the head, with the noise and the sealed split."""

import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from pytest_mock import MockerFixture
from sqlalchemy import select

from piloop.actor import Agent
from piloop.campaign.ops import Refused
from piloop.daemon.main import Daemon
from piloop.db.events import in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment
from piloop.db.session import Database
from piloop.ids import Revision
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.jobs import InfraFailed
from piloop.plugins.campaign_type.kit.evaluation.measure import Split, current_evaluator, noise, sealed_evaluator
from piloop.plugins.campaign_type.kit.evaluation.submissions import SubmitRefused, submit, waiting
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.controller import UNSUBMITTED, Measuring, RunController, close_run
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunEnding, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.ops import end_run
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.campaign_type.kit.verdicts.judge import CandidateJudged, head, short_of_noise
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
    wait_until,
    workers_of,
    write_toml,
)
from tests.daemon.test_evaluation_queue import queued_executor
from tests.daemon.test_run_controller import controller, keep_elsewhere, make_run, run_row, set_config

SUBMIT = {'scoring': 'submit', 'noise_runs': 1}
NOISE = {'verdict_rule': {'provider': 'noise'}}


def worktree(campaign: Campaign, tmp_path: Path) -> Path:
    """An implementer's checkout of the campaign's head."""
    return worktree_of(campaign, tmp_path, 'checkout')


def worktree_of(campaign: Campaign, tmp_path: Path, name: str) -> Path:
    """An implementer's checkout of the campaign's head, called ``name``."""
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


def verdict(db: Database, candidate_id: CandidateId) -> tuple[Outcome | None, str | None]:
    with db.session() as s:
        c = s.get_one(Candidate, candidate_id)
        return c.verdict, c.verdict_reason


def judged(db: Database, candidate_id: CandidateId) -> CandidateJudged:
    with db.session() as s:
        [*_, (_, last)] = [e for e in in_order(s, CandidateJudged) if e[0].target_id == candidate_id]
        return last


@pytest.fixture
def ex(db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path) -> RunController:
    set_config(db, campaign, evaluator=SUBMIT, plugins=NOISE)
    return controller(db, runtime, tmp_path)


def test_a_submission_that_beats_the_head_becomes_it_and_one_that_beats_only_its_parent_is_recorded(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    better = submitted(db, campaign, run_id, commit(checkout, 'base\na\nb\n'))
    with db.session() as s:
        assert [c.id for c in waiting(s, s.get_one(Campaign, campaign.id))] == [better], 'it waits for its verdict'
    ex.judge_submission(better)
    assert verdict(db, better)[0] is Outcome.KEEP
    assert (judged(db, better).parent_delta, judged(db, better).best_delta) == (2.0, 2.0)
    worse = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    ex.judge_submission(worse)
    assert verdict(db, worse)[0] is Outcome.DISCARD, 'it beats the baseline, not the head'
    assert (judged(db, worse).parent_delta, judged(db, worse).best_delta) == (-1.0, -1.0)
    with db.session() as s:
        trunk = head(s, s.get_one(Campaign, campaign.id))
        assert trunk is not None and trunk.id == better
        assert s.get_one(Candidate, worse).parent_id == better, 'a submission builds on the last one'


def test_a_run_submits_at_most_its_cap_and_never_a_known_tree(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    first = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    ex.judge_submission(first)
    commit(checkout, 'base\nb\n')
    again = commit(checkout, 'base\na\n')
    assert submitted(db, campaign, run_id, again) == first, 'its own submission again: no submission spent'
    with pytest.raises(SubmitRefused, match=r'its tree is candidate#1 already \(keep\)'):
        submitted(db, campaign, run_id, commit(checkout, 'base\n'))
    submitted(db, campaign, run_id, commit(checkout, 'base\nc\n'))
    submitted(db, campaign, run_id, commit(checkout, 'base\nd\n'))
    with pytest.raises(SubmitRefused, match=rf'run#{run_id} used its 3 submissions'):
        submitted(db, campaign, run_id, commit(checkout, 'base\ne\n'))


def test_a_run_that_claimed_no_idea_submits_nothing(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """Judged on no idea's track, its version could move the head for nothing on the board."""
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
    """The newest version is skipped, with no verdict: the run's idea is done on its submission's."""
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    kept = commit(checkout, 'base\na\n')
    ex.judge_submission(submitted(db, campaign, run_id, kept))
    later = commit(checkout, 'base\na\nb\n')
    with closing(Git().open(Path(campaign.path))) as repo:
        versions = [repo.version(kept), repo.version(later)]
        last = ex._score(run_id, campaign.id, repo, versions, tmp_path / 'logs')
    with db.session() as s:
        [one, two] = s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)).all()
        assert (one.verdict, two.origin, two.verdict_reason, two.measurements) == (
            Outcome.KEEP,
            Origin.SKIPPED,
            UNSUBMITTED,
            [],
        )
        assert last == one.id
        close_run(s, s.get_one(Run, run_id), RunEnd.FINISHED, None).scoring = True
        s.commit()
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


def test_the_noise_of_the_baseline_decides_and_a_gain_within_it_is_measured_once_more(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The baseline scores 10, 12, 14: σ = 2. The first submission's 15 is within 2σ, so it runs again: 19, a mean of 17
    beyond 2σ/√2."""
    set_config(
        db,
        campaign,
        evaluate=queue_of(tmp_path, 10, 12, 14, 15, 19),
        evaluator={**SUBMIT, 'noise_runs': 3},
        plugins=NOISE,
    )
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, p, p.config).id
        assert noise(s.get_one(Candidate, baseline(db, campaign.id).id), evaluator, 'score') == 2.0
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (
        Outcome.KEEP,
        'against candidate#1: score 17 vs 12 (+5; noise 2.828 over 2 evaluations)',
    )


SEALED = ['sh', '-c', reports(score='$(( 10 - $(wc -l < solve.txt) ))', size='1')]
"""A sealed split that disagrees: each line costs a point there."""


def test_a_keep_must_hold_on_the_sealed_split_which_scores_only_a_would_be_keep(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED}, plugins=NOISE)
    ex = controller(db, runtime, tmp_path)
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    worse = submitted(db, campaign, run_id, commit(checkout, 'base\n'.replace('base', 'other')))
    ex.judge_submission(worse)
    better = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    ex.judge_submission(better)
    assert verdict(db, better) == (
        Outcome.DISCARD,
        'against candidate#1: score 2 vs 1 (+1; noise 0 over 1 evaluations); the gain does not hold on the sealed split',
    )
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None and hidden.id != p.evaluator_id
        scored = set(s.scalars(select(Measurement.candidate_id).where(Measurement.evaluator_id == hidden.id)))
        assert scored == {1, better}, 'only the would-be keep and the head it would replace'


HARNESS_SUBMITS = '#!/bin/sh\necho a >> solve.txt\ngit commit -qam more\n"$PILOOP_BIN" submit > ../submitted.txt 2>&1\n"$PILOOP_BIN" run end submitted\n'
"""Commits one line, submits it, keeps what ``piloop submit`` printed in its log directory, and ends its run."""


def test_an_implementer_submits_through_the_daemon_and_reads_its_verdict_while_it_works(
    campaign_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_toml(campaign_path, HARNESS_SUBMITS)
    home = Settings(home=tmp_path / 'home')
    daemon = Daemon(home)
    try:
        workers_of(daemon.controller).worker.runtime_factory = factory(ScriptRuntime())
        campaign = register(daemon.db, campaign_path)
        set_config(daemon.db, campaign, evaluator=SUBMIT, plugins=NOISE)
        run_id = make_run(daemon.db, campaign)
        with serving_api(home):
            daemon.scheduler.reap()
            run_all(daemon.scheduler)
            wait_until(lambda: waiting_of(daemon.db, campaign), 'the submission')
            judge, go, judged = daemon.controller.judge_submission, threading.Event(), []

            def held(candidate_id: CandidateId) -> None:
                judged.append(candidate_id)
                go.wait(30)
                judge(candidate_id)

            monkeypatch.setattr(daemon.controller, 'judge_submission', held)
            run_all(daemon.scheduler)  # what its event wakes in the daemon's loop
            run_all(daemon.scheduler)  # while it is judged: not judged twice
            go.set()
            wait_until(lambda: run_row(daemon.db, run_id).status is RunStatus.SUCCEEDED, 'the run', timeout=120)
            daemon.scheduler.wait(SETTLE)
        with daemon.db.session() as s:
            [kept] = s.scalars(select(Candidate).where(Candidate.run_id == run_id)).all()
            assert (kept.verdict, kept.origin) == (Outcome.KEEP, Origin.RUN), 'judged while the run worked'
            assert judged == [kept.id], 'once'
    finally:
        daemon.shutdown(timeout=5)
    assert (home.home / 'runs' / str(run_id) / 'submitted.txt').read_text() == 'keep 2\n'


def waiting_of(db: Database, campaign: Campaign) -> list[Candidate]:
    with db.session() as s:
        return waiting(s, s.get_one(Campaign, campaign.id))


def test_a_submission_that_the_head_passes_while_it_is_measured_is_judged_against_the_new_head(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, checkout = running(db, campaign), worktree(campaign, tmp_path)
    candidate = submitted(db, campaign, run_id, commit(checkout, 'base\na\n'))
    moved: list[CandidateId] = []

    def head_moves_meanwhile[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
        def ready(*args: P.args, **kwargs: P.kwargs) -> R:
            done = fn(*args, **kwargs)
            if not moved:
                files, scored = {'other.txt': 'x\n'}, {'score': 5.0, 'size': 1.0}
                moved.append(keep_elsewhere(db, campaign, 'elsewhere', files, scored))
            return done

        return ready

    monkeypatch.setattr(ex, '_ready', head_moves_meanwhile(ex._ready))
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (
        Outcome.DISCARD,
        f'against candidate#{moved[0]}: score 2 vs 5 (-3; noise 0 over 1 evaluations)',
    ), 'judged against the head that it would replace'


def test_a_submission_whose_evaluation_halts_waits_for_its_verdict(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    monkeypatch.setattr(ex, '_measure', lambda *args, **kwargs: Measuring.STOPPED)
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (None, None), 'the next daemon judges it'


def test_a_submission_that_piloop_fails_to_judge_waits_for_its_verdict(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))

    def broken(*args: object, **kwargs: object) -> Measuring:
        raise RuntimeError('the repo is gone')

    monkeypatch.setattr(ex, '_ready', broken)
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (None, None), 'a fault of piloop is no verdict: the next pass judges it again'


def test_a_re_run_that_a_stop_ends_leaves_the_submission_waiting(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_config(
        db, campaign, evaluate=queue_of(tmp_path, 10, 12, 14, 15), evaluator={**SUBMIT, 'noise_runs': 3}, plugins=NOISE
    )
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    calls: list[object] = []

    def first_only[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
        def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
            calls.append(args)
            return fn(*args, **kwargs) if len(calls) == 1 else Measuring.STOPPED

        return measured

    monkeypatch.setattr(ex, '_measure', first_only(ex._measure))
    ex.judge_submission(candidate)
    assert (len(calls), verdict(db, candidate)) == (2, (None, None)), 'its 15 is within 2σ: the re-run stopped'


def test_the_sealed_evaluator_that_another_judge_inserted_first_is_the_one_both_use(
    db: Database, campaign: Campaign, mocker: MockerFixture
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        first = sealed_evaluator(s, p, p.config)
        s.commit()
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        mocker.patch.object(s, 'scalar', return_value=None)  # it looked before the other judge's insert
        raced = sealed_evaluator(s, p, p.config)
        assert first is not None and raced is not None and raced.id == first.id


def test_a_sealed_score_that_a_stop_ends_leaves_the_submission_waiting(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED}, plugins=NOISE)
    ex = controller(db, runtime, tmp_path)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))

    def sealed_stops[**P](fn: Callable[P, Measuring]) -> Callable[P, Measuring]:
        def measured(*args: P.args, **kwargs: P.kwargs) -> Measuring:
            return Measuring.STOPPED if kwargs.get('split') is Split.SEALED else fn(*args, **kwargs)

        return measured

    monkeypatch.setattr(ex, '_measure', sealed_stops(ex._measure))
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (None, None)


def scoring(db: Database, run_id: RunId) -> bool:
    with db.session() as s:
        return s.get_one(Run, run_id).scoring


def test_a_run_that_ends_while_its_submission_is_judged_settles_on_its_verdict(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """However it ends, a lost worker or no version at all, its idea waits for the verdict; a stop leaves it scoring."""
    run_id = running(db, campaign)
    candidate = submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n'))
    with db.session() as s:
        close_run(s, s.get_one(Run, run_id), RunEnd.WORKER_LOST, 'lost').scoring = True
        s.commit()
    settling = threading.Thread(target=ex._settle, args=(run_id, RunEnd.WORKER_LOST, None))
    settling.start()
    settling.join(0.5)
    assert settling.is_alive() and scoring(db, run_id), 'it waits for the verdict'
    ex.judge_submission(candidate)
    settling.join(30)
    assert not scoring(db, run_id)
    other = running(db, campaign, 'other')
    submitted(db, campaign, other, commit(worktree_of(campaign, tmp_path, 'second'), 'base\nb\n'))
    ex.launcher.cancel()
    ex._settle(other, RunEnd.FINISHED, None)
    with db.session() as s:
        assert s.get_one(Run, other).status is RunStatus.RUNNING, 'a stop settles nothing: the next daemon does'


def test_the_gain_over_the_parent_and_over_the_head_are_recorded_apart(
    db: Database, campaign: Campaign, ex: RunController, tmp_path: Path
) -> None:
    """The head moved past the run's base: the submission beats its parent, not the head, and is recorded so."""
    run_id = running(db, campaign)
    head_id = keep_elsewhere(db, campaign, 'elsewhere', {'other.txt': 'x\n'}, {'score': 3.0, 'size': 1.0})
    candidate = submitted(db, campaign, run_id, commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.judge_submission(candidate)
    assert verdict(db, candidate)[0] is Outcome.DISCARD
    assert (judged(db, candidate).parent_delta, judged(db, candidate).best_delta) == (1.0, -1.0), f'head {head_id}'


def test_the_noise_counts_only_the_baselines_successful_evaluations_and_an_unknown_one_keeps_nothing(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A crash is no sample: one success gives no noise, which is escalated once, and the submission is interesting."""
    script = queue_of(tmp_path, 10, 0, 12)
    crash = f'{script[2]}; [ "$v" = 0 ] && exit 3; true'
    set_config(db, campaign, evaluate=['sh', '-c', crash], evaluator={**SUBMIT, 'noise_runs': 3}, plugins=NOISE)
    ex = controller(db, runtime, tmp_path)
    base = baseline(db, campaign.id)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, p, p.config).id
        assert short_of_noise(s.get_one(Candidate, base.id), evaluator, p) == 3, 'none under the new evaluator'
    ex.remeasure(base.id)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        first = s.get_one(Candidate, base.id)
        assert [m.error is None for m in first.measurements if m.evaluator_id == evaluator] == [True, False]
        assert short_of_noise(first, evaluator, p) == 2, 'a crash is no sample'
        for _ in range(2):
            s.add(Measurement(candidate_id=base.id, evaluator_id=evaluator, error='crashed'))
        s.flush()
        s.expire(first)
        assert short_of_noise(first, evaluator, p) == 0, 'as many crashes as noise_runs: no more asks'
        s.commit()
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.judge_submission(candidate)
    assert verdict(db, candidate)[0] is Outcome.INTERESTING
    with db.session() as s:
        escalations = s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all()
        assert [c.body.splitlines()[0] for c in escalations] == [
            'The noise of score is unknown, so no candidate of demo can be kept. What now?'
        ]


SEALED_CRASHES_ON_THE_HEAD = ['sh', '-c', f'[ "$(wc -l < solve.txt)" -eq 1 ] && exit 9; {reports(score="1", size="1")}']
"""A sealed command that crashes on the head's one line. A number test: BSD wc pads its count with spaces."""
"""A sealed split that crashes on the head's tree, one line, every time."""


def test_a_sealed_split_that_crashes_on_the_head_runs_again_once_and_is_never_a_pass(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED_CRASHES_ON_THE_HEAD}, plugins=NOISE)
    ex = controller(db, runtime, tmp_path)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    ex.judge_submission(candidate)
    assert verdict(db, candidate) == (
        Outcome.INTERESTING,
        'against candidate#1: score 2 vs 1 (+1; noise 0 over 1 evaluations); the sealed split did not score candidate#1',
    )
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None
        tries = s.scalars(
            select(Measurement.error).where(Measurement.evaluator_id == hidden.id, Measurement.candidate_id == 1)
        )
        assert len(tries.all()) == 2, 'the head ran on the split twice'


BAD_SEALED = [
    'sh',
    '-c',
    'printf \'%s\\n\' \'{"status": "ok", "score": 1, "direction": "lower"}\' >> "$PILOOP_RESULT_FILE"',
]
"""A sealed command whose result piloop refuses: the campaign's primary, score, is higher-is-better."""
SEALED_AGREES = ['sh', '-c', reports(score='$(wc -l < solve.txt)', size='1')]
"""A sealed command that scores as the visible split does."""


def test_a_sealed_result_that_piloop_refuses_is_no_verdict_and_escalates_once_until_the_command_is_fixed(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A candidate cannot change the sealed command: its bad result is the setup's fault, never a discard. The
    submission waits while its sealed evaluation runs again, one escalation says why, and a fixed command judges it."""
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': BAD_SEALED}, plugins=NOISE)
    ex = queued_executor(db, runtime, tmp_path, 1)
    candidate = submitted(db, campaign, running(db, campaign), commit(worktree(campaign, tmp_path), 'base\na\n'))
    judging = threading.Thread(target=ex.judge_submission, args=(candidate,))
    judging.start()
    wait_until(lambda: len(failures(db)) >= 2, 'the sealed evaluation failed twice, and waits to run again')
    assert verdict(db, candidate) == (None, None), 'no verdict, and no discard'
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None
        assert s.scalars(select(Measurement).where(Measurement.evaluator_id == hidden.id)).all() == []
        [escalation] = s.scalars(select(Comment).where(Comment.kind == ESCALATION)).all()
        assert 'it reports the direction lower; [[metrics]] says score is higher' in escalation.body
    set_config(db, campaign, evaluator={**SUBMIT, 'sealed': SEALED_AGREES}, plugins=NOISE)
    judging.join(30)
    assert not judging.is_alive(), 'a new sealed command ends the wait for the old one'
    ex.judge_submission(candidate)
    assert verdict(db, candidate)[0] is Outcome.KEEP


def failures(db: Database) -> list[InfraFailed]:
    with db.session() as s:
        return [e for _, e in in_order(s, InfraFailed)]


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
