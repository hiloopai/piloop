"""The evaluation queue: one evaluation per tree, a cap on the evaluations that run, a fair order, and cancels."""

import json
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from git import Repo
from pytest_mock import MockerFixture
from sqlalchemy import delete, select

from piloop.actor import System
from piloop.db.events import in_order, latest
from piloop.db.models import PROCESS, Campaign, Event, utcnow
from piloop.db.session import Database
from piloop.ids import TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId
from piloop.plugins.campaign_type.kit.evaluation.jobs import KIND, InfraFailed
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    current_evaluator,
    measured,
    request_remeasure,
    retired,
    sealed_evaluator,
)
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluation, Measurement
from piloop.plugins.campaign_type.kit.pool.due import due
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.executor.workers import Workers
from piloop.plugins.vcs.git import commands as git
from piloop.settings import EvaluationSettings
from tests.conftest import (
    SETTLE,
    ScriptRuntime,
    bare_config,
    baseline,
    factory,
    reports,
    run_all,
    serving,
    store_config,
    wait_until,
    workers_of,
    write_script,
)
from tests.daemon.test_run_controller import execute, make_run, set_config
from tests.daemon.test_scheduler import add_ideas, make_scheduler

HARNESS_OWN_LINE = '#!/bin/sh\nbasename "$(dirname "$PWD")" >> solve.txt\ngit commit -qam "own line"\n'
"""Each run appends its own id (``runs/<id>/worktree``), so each run makes a tree of its own."""
TOLD = 'score 2 over 1 eval: the best of the score track now, rank 1 of 2; vs parent +1'
"""What a run's worker hears of its commit of one line more than the baseline's."""
SCORE = reports(score='$(wc -l < solve.txt)', size='$(wc -c < solve.txt)')


def held_evaluate(tmp_path: Path, then: str = SCORE) -> list[str]:
    """An evaluate command that logs its commit in ``evaluated``, waits while ``hold`` exists, then runs ``then``."""
    log, hold = tmp_path / 'evaluated', tmp_path / 'hold'
    return ['sh', '-c', f'git rev-parse HEAD >> {log}; while [ -f {hold} ]; do sleep 0.05; done; {then}']


def held_runs(db: Database, campaign: Campaign, tmp_path: Path, then: str = SCORE) -> None:
    """``held_evaluate`` as a new evaluator that has measured the baseline: the runs' bar, which the scheduler measures
    before any run starts. So each evaluation that the test holds is a run's own."""
    set_config(db, campaign, evaluate=held_evaluate(tmp_path, then))
    bar = baseline(db, campaign.id).id
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, proj, proj.config).id
        s.add(Measurement(candidate_id=bar, evaluator_id=evaluator, metrics={'score': 1.0, 'size': 5.0}))
        s.commit()


def rows(db: Database) -> list[tuple[int, bool, str | None]]:
    """``(candidate, started, cancelled_by)`` of each evaluation in the queue, by id."""
    with db.session() as s:
        found = s.scalars(select(Evaluation).order_by(Evaluation.id)).all()
        return [(e.candidate_id, e.started_at is not None, e.cancelled_by) for e in found]


def in_thread(fn: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=fn)
    thread.start()
    return thread


def evaluated(tmp_path: Path) -> list[str]:
    path = tmp_path / 'evaluated'
    return path.read_text().split() if path.exists() else []


def run_candidates(db: Database, run_id: RunId) -> list[Candidate]:
    with db.session() as s:
        return list(s.scalars(select(Candidate).where(Candidate.run_id == run_id).order_by(Candidate.id)))


RETRY = EvaluationSettings(infra_retry=timedelta(milliseconds=50))
"""An infra error asks again at once: the tests wait for no real minute."""


def queued_executor(
    db: Database, runtime: ScriptRuntime, tmp_path: Path, slots: int, evaluation: EvaluationSettings = RETRY
) -> RunController:
    executor = Workers.model_validate({'pools': {'serve': {'slots': {'evaluation': slots}}}})
    controller = RunController(db, Tracer(None), tmp_path, factory(runtime), executor=executor, evaluation=evaluation)
    return serving(controller)


def errors(db: Database, candidate_id: CandidateId) -> tuple[list[str | None], str | None]:
    """The errors of the candidate's measurements, and where its last score told it stands, or None."""
    with db.session() as s:
        c = s.get_one(Candidate, candidate_id)
        found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate_id))
        return [m.error for m in c.measurements], found[1].told if found is not None else None


def test_two_runs_that_reach_one_tree_share_one_evaluation_and_its_crash(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A tree that a second run reaches while it is evaluated is not evaluated again, not even after a crash."""
    (tmp_path / 'hold').touch()
    held_runs(db, campaign, tmp_path, then='exit 7')
    ex = queued_executor(db, runtime, tmp_path, slots=2)
    waits = mocker.spy(queue, 'request')
    first, second = make_run(db, campaign), make_run(db, campaign, title='the same line')
    one = in_thread(lambda: execute(ex, db, first))
    wait_until(lambda: len(evaluated(tmp_path)) == 1, 'the first evaluation')
    two = in_thread(lambda: execute(ex, db, second))
    wait_until(lambda: waits.call_count == 2, 'the second request')
    (tmp_path / 'hold').unlink()
    one.join(30)
    two.join(30)
    [candidate] = run_candidates(db, first)
    assert run_candidates(db, second) == [], 'the second run reached the tree of the first'
    assert errors(db, candidate.id) == (['evaluate exit 7: '], 'crash: no evaluation succeeded')
    assert len(evaluated(tmp_path)) == 1, 'evaluated once'
    assert rows(db) == [], 'the queue is empty'


@pytest.mark.usefixtures('submitting')
def test_a_shared_evaluation_that_cannot_start_is_no_measurement_and_runs_again_for_every_request(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A worktree that piloop cannot make is not the candidate's fault: both requests share what it measures later."""
    ex = queued_executor(db, runtime, tmp_path, slots=2)
    waits = mocker.spy(queue, 'request')
    add_worktree = git.add_worktree
    failed: list[Path] = []

    def cannot_check_out(repo: Repo, path: Path, sha: str, links: dict[str, Path]) -> None:
        if 'eval-' not in str(path) or failed:  # the implementer's worktree, or the evaluation that runs again
            return add_worktree(repo, path, sha, links)
        wait_until(lambda: waits.call_count == 2, 'the second request')
        failed.append(path)
        raise RuntimeError('cannot check out the commit')

    mocker.patch('piloop.plugins.vcs.git.commands.add_worktree', side_effect=cannot_check_out)
    first, second = make_run(db, campaign), make_run(db, campaign, title='the same line')
    one = in_thread(lambda: execute(ex, db, first))
    wait_until(lambda: len(rows(db)) == 1, 'the first request')
    two = in_thread(lambda: execute(ex, db, second))
    one.join(30)
    two.join(30)
    [candidate] = run_candidates(db, first)
    assert errors(db, candidate.id) == ([None], TOLD), 'measured once, after the infra error'
    with db.session() as s:
        assert [s.get_one(Run, r).status for r in (first, second)] == [RunStatus.SUCCEEDED] * 2
        [infra] = [p for _, p in in_order(s, InfraFailed)]
        assert infra.error == 'RuntimeError: cannot check out the commit'
    assert rows(db) == []


def test_a_cancelled_evaluation_is_no_score_and_its_candidate_is_skipped_at_once(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A cancel skips the candidate itself, so it holds even if no daemon sees it; a running evaluator is stopped."""
    write_script(Path(campaign.path).parent / 'harness.sh', HARNESS_OWN_LINE)
    (tmp_path / 'hold').touch()
    held_runs(db, campaign, tmp_path)
    ex = queued_executor(db, runtime, tmp_path, slots=1)
    first, second = make_run(db, campaign), make_run(db, campaign, title='another line')
    one = in_thread(lambda: execute(ex, db, first))
    wait_until(lambda: len(evaluated(tmp_path)) == 1, 'the first evaluation')
    two = in_thread(lambda: execute(ex, db, second))
    wait_until(lambda: len(rows(db)) == 2, 'the second evaluation to wait')
    [running] = run_candidates(db, first)
    [waiting] = run_candidates(db, second)
    with db.session() as s:
        assert queue.cancel(s, waiting.id, System.ORCHESTRATOR) == 1
        assert queue.cancel(s, running.id, System.HUMAN) == 1
        s.commit()
    assert rows(db) == [(running.id, True, 'human')], 'the waiting one left the queue; the running one is stopping'
    two.join(30)
    ex.stop_cancelled()
    one.join(30)
    assert skipped(db, running.id) == []
    assert skipped(db, waiting.id) == []
    assert errors(db, running.id)[1] is None and errors(db, waiting.id)[1] is None, 'no score'
    assert len(evaluated(tmp_path)) == 1, 'the waiting evaluation never started'
    assert rows(db) == []


def skipped(db: Database, candidate_id: CandidateId) -> list[Measurement]:
    """The measurements of a ``skipped`` candidate."""
    with db.session() as s:
        c = s.get_one(Candidate, candidate_id)
        assert c.origin is Origin.SKIPPED
        return list(c.measurements)


def test_a_cancel_holds_when_the_daemon_restarts_before_it_sees_it(db: Database, campaign: Campaign) -> None:
    base = baseline(db, campaign.id).id
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        other = Candidate(campaign_id=campaign.id, tree_sha='t', revision='c')
        s.add(other)
        s.flush()
        running, _ = queue.request(s, base, evaluator)
        running.started_at = utcnow()  # a worker took it
        queue.request(s, other.id, evaluator)
        queue.cancel(s, base, System.HUMAN)
        queue.cancel(s, other.id, System.HUMAN)
        s.commit()
        s.execute(delete(Evaluation))  # what the next daemon's reap does
        s.commit()
        assert not measured(s.get_one(Candidate, other.id), evaluator)
    assert skipped(db, other.id) == [], 'no daemon measures it'


def test_a_stop_ends_the_waiting_evaluations_and_records_nothing(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    write_script(Path(campaign.path).parent / 'harness.sh', HARNESS_OWN_LINE)
    (tmp_path / 'hold').touch()
    held_runs(db, campaign, tmp_path)
    ex = queued_executor(db, runtime, tmp_path, slots=1)
    first, second = make_run(db, campaign), make_run(db, campaign, title='another line')
    threads = [in_thread(lambda: execute(ex, db, first))]
    wait_until(lambda: len(evaluated(tmp_path)) == 1, 'the first evaluation')
    threads.append(in_thread(lambda: execute(ex, db, second)))
    wait_until(lambda: len(rows(db)) == 2, 'the second evaluation to wait')
    ex.stop(30)
    for thread in threads:
        thread.join(30)
    workers_of(ex).worker.join(30)
    with db.session() as s:
        assert [c.measurements for c in s.scalars(select(Candidate).where(Candidate.run_id.is_not(None)))] == [[], []]
    [_, waiting] = run_candidates(db, first) + run_candidates(db, second)
    assert rows(db) == [(waiting.id, False, None)], (
        "the stopped one left the queue; the waiting one stays queued for the next daemon's worker"
    )


def test_an_evaluation_that_waits_for_a_slot_gives_up_when_the_database_closes(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A closed database answers every wait at once: the wait ends, it never spins on it."""
    executor = Workers.model_validate({'pools': {'serve': {'slots': {'evaluation': 1}}}})
    ex = RunController(db, Tracer(None), tmp_path, factory(runtime), executor=executor)  # no worker: no free slot
    base = baseline(db, campaign.id).id
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        evaluation, _ = queue.request(s, base, evaluator)
        s.commit()
        evaluation_id = evaluation.id
    waking = mocker.spy(ex, '_next_wake')
    waited: list[bool] = []
    waiter = in_thread(lambda: waited.append(ex._wait(evaluation_id)))
    # the doorbell's first read wakes the first wait once; the second lasts until the close
    wait_until(lambda: waking.call_count == 2, 'the wait for a slot')
    db.close()
    waiter.join(SETTLE)
    assert waited == [False], 'it gave up'


def add_campaign(db: Database, name: str, trees: int) -> list[CandidateId]:
    """A second campaign with ``trees`` candidates, none kept."""
    with db.session() as s:
        other = Campaign(name=name, path=f'/{name}', config=bare_config(name))
        s.add(other)
        s.flush()
        made = [Candidate(campaign_id=other.id, tree_sha=f'{name}{i}', revision=f'{name}{i}') for i in range(trees)]
        s.add_all(made)
        s.commit()
        return [c.id for c in made]


def test_a_busy_campaign_never_holds_back_another_campaign(db: Database, campaign: Campaign) -> None:
    """A free slot goes to the campaign with the fewest running evaluations, then to the oldest request.

    A strict class order let one campaign's stream of run evaluations hold back another campaign's re-measure for ever.
    """
    busy = add_campaign(db, 'busy', 8)
    [third] = add_campaign(db, 'third', 1)
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None

        def ask(candidate_id: CandidateId) -> Evaluation:
            return queue.request(s, candidate_id, evaluator)[0]

        def finish(evaluation: Evaluation) -> None:
            s.delete(evaluation)
            s.flush()

        candidate = Candidate(campaign_id=campaign.id, tree_sha='t', revision='c')
        s.add(candidate)
        s.flush()

        def take() -> Evaluation:
            """What a worker with a free slot does: it starts the next one (``Leases.acquire``)."""
            evaluation = queue.waiting(s)[0]
            evaluation.started_at = utcnow()
            s.flush()
            return evaluation

        first = ask(busy[0])
        assert take() == first
        older = ask(busy[1])
        quiet = ask(candidate.id)
        newer = [ask(c) for c in busy[2:5]]
        assert [e.id for e in queue.waiting(s)] == [quiet.id, older.id, *(e.id for e in newer)], (
            'while the busy campaign runs one, the quiet campaign is next'
        )
        finish(first)
        assert take() == older, 'the request before the quiet campaign: its turn first'
        finish(older)
        assert take() == quiet, 'the busy campaign asked again meanwhile, but the quiet one is next'
        assert take() == newer[0]
        late = ask(busy[5])
        mine = ask(third)
        assert [e.id for e in queue.waiting(s)] == [mine.id, newer[1].id, newer[2].id, late.id], (
            'two slots: the campaign with nothing running first, then the oldest'
        )
        assert [e.id for e in queue.listing(s)] == [quiet.id, newer[0].id, mine.id, newer[1].id, newer[2].id, late.id]


def test_a_campaign_takes_its_baseline_before_its_older_requests(db: Database, campaign: Campaign) -> None:
    base = baseline(db, campaign.id).id
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        other = Candidate(campaign_id=campaign.id, tree_sha='t', revision='c')
        s.add(other)
        s.flush()
        older = queue.request(s, other.id, evaluator)[0]
        first, created = queue.request(s, base, evaluator)
        assert created and queue.request(s, first.candidate_id, evaluator) == (first, False), 'one row per tree'
        assert [e.id for e in queue.waiting(s)] == [first.id, older.id]


def test_an_evaluation_id_is_never_reused(db: Database, campaign: Campaign) -> None:
    """A request that waits for an evaluation by id never mistakes a later one for it."""
    base = baseline(db, campaign.id).id
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        gone = queue.request(s, base, evaluator)[0].id
        s.commit()
        s.execute(delete(Evaluation))
        s.commit()
        assert queue.request(s, base, evaluator)[0].id > gone


def test_evaluations_that_wait_for_a_slot_never_hold_back_an_agent_launch(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run waits in the evaluation queue on its pool thread, so the pool must not run out while many wait."""
    scheduler = make_scheduler(db, runtime, tmp_path)
    release = threading.Event()
    waiting = [scheduler._pool.submit(release.wait, 30) for _ in range(64)]  # more than any default pool size
    add_ideas(db, campaign, 'one line')
    try:
        assert len(run_all(scheduler)) == 1
        with db.session() as s:
            [run_id] = s.scalars(select(Run.id)).all()
        wait_until(lambda: run_status(db, run_id) is not RunStatus.QUEUED, 'the agent to start')
    finally:
        release.set()
    assert all(f.result(5) for f in waiting)
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)


def run_status(db: Database, run_id: RunId) -> RunStatus:
    with db.session() as s:
        return s.get_one(Run, run_id).status


def test_a_remeasure_whose_repo_is_gone_is_a_crash(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    Path(campaign.path).rename(tmp_path / 'moved')
    base = baseline(db, campaign.id).id
    queued_executor(db, runtime, tmp_path, slots=1).remeasure(base)
    found, told = errors(db, base)
    assert found[-1] is not None and found[-1].startswith('NotARepository'), 'it cannot start: a crash, asked again'
    assert told is None, "the baseline is no run's: nobody hears it"
    with db.session() as s:
        best = best_of(s, s.get_one(Campaign, campaign.id))
        assert best is not None and best.id == base, 'the baseline is still the best'
    assert rows(db) == []


def killed_first(tmp_path: Path, times: int, sig: str = 'TERM') -> list[str]:
    """An evaluate command that logs each try in ``tries`` and kills itself with ``sig`` on its first ``times`` tries."""
    tries = tmp_path / 'tries'
    return ['sh', '-c', f'echo x >> {tries}; [ $(wc -l < {tries}) -gt {times} ] || kill -{sig} $$; {SCORE}']


def tries(tmp_path: Path) -> int:
    return len((tmp_path / 'tries').read_text().split())


def test_an_evaluation_that_another_process_kills_is_run_once_more(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """An implementer's ``pkill -f`` measures nothing of the candidate, so the daemon evaluates it again, once."""
    set_config(db, campaign, evaluate=killed_first(tmp_path, times=1))
    base = baseline(db, campaign.id).id
    queued_executor(db, runtime, tmp_path, slots=1).remeasure(base)
    assert tries(tmp_path) == 2
    with db.session() as s:
        last = s.get_one(Candidate, base).measurements[-1]
        assert (last.error, last.metrics) == (None, {'score': 1.0, 'size': 5.0})
    assert rows(db) == []


def infra_first(tmp_path: Path, times: int) -> list[str]:
    """An evaluate command that logs each try in ``tries`` and reports an infra error on its first ``times`` tries."""
    tries, infra = tmp_path / 'tries', json.dumps({'status': 'infra_error', 'feedback': 'HTTP 429'})
    said = f"printf '%s\\n' '{infra}' >> \"$PILOOP_RESULT_FILE\"; exit 1"
    return ['sh', '-c', f'echo x >> {tries}; if [ $(wc -l < {tries}) -le {times} ]; then {said}; fi; {SCORE}']


def test_an_infra_error_is_no_measurement_and_runs_again_until_it_measures(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluate=infra_first(tmp_path, times=2))
    base = baseline(db, campaign.id).id
    with db.session() as s:
        before = len(s.get_one(Candidate, base).measurements)
    queued_executor(db, runtime, tmp_path, slots=1).remeasure(base)
    assert tries(tmp_path) == 3
    with db.session() as s:
        [after] = s.get_one(Candidate, base).measurements[before:]
        assert (after.error, after.metrics) == (None, {'score': 1.0, 'size': 5.0}), 'only the measurement counts'
        assert [p.error for _, p in in_order(s, InfraFailed)] == ['evaluate reported infra_error: HTTP 429'] * 2
    assert rows(db) == []


def test_a_daemon_that_stops_while_an_infra_error_waits_asks_no_more(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluate=infra_first(tmp_path, times=5))
    ex = queued_executor(db, runtime, tmp_path, 1, EvaluationSettings(infra_retry=timedelta(minutes=10)))
    remeasured = in_thread(lambda: ex.remeasure(baseline(db, campaign.id).id))
    base = baseline(db, campaign.id).id
    wait_until(lambda: (tmp_path / 'tries').exists() and rows(db) == [(base, False, None)], 'the wait in the queue')
    ex.stop(30)
    remeasured.join(30)
    assert not remeasured.is_alive(), 'the stop ends the wait'
    assert tries(tmp_path) == 1


def test_an_evaluation_killed_twice_waits_and_runs_again_as_an_infra_error(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    set_config(db, campaign, evaluate=killed_first(tmp_path, times=2))
    base = baseline(db, campaign.id).id
    with db.session() as s:
        before = len(s.get_one(Candidate, base).measurements)
    queued_executor(db, runtime, tmp_path, slots=1).remeasure(base)
    assert tries(tmp_path) == 3, 'killed twice in one evaluation, then measured in the next'
    with db.session() as s:
        [after] = s.get_one(Candidate, base).measurements[before:]
        assert after.error is None
        [infra] = [p for _, p in in_order(s, InfraFailed)]
        assert infra.error == 'evaluate killed by SIGTERM (exit -15), not by its own exit: '


def test_an_evaluation_that_waits_after_an_infra_error_is_listed_and_a_cancel_ends_it(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The wait doubles after each failure, and the evaluation waits in the queue, where a cancel finds it."""
    set_config(db, campaign, evaluate=infra_first(tmp_path, times=99))
    wait = EvaluationSettings(infra_retry=timedelta(milliseconds=50), infra_retry_max=timedelta(minutes=10))
    ex = queued_executor(db, runtime, tmp_path, 1, wait)
    base = baseline(db, campaign.id).id
    remeasured = in_thread(lambda: ex.remeasure(base))
    wait_until(lambda: tries(tmp_path) >= 3 if (tmp_path / 'tries').exists() else False, 'two retries')
    wait_until(lambda: rows(db) == [(base, False, None)], 'the wait in the queue')
    with db.session() as s:
        assert queue.cancel(s, base, System.HUMAN) == 1
        s.commit()
    remeasured.join(30)
    assert not remeasured.is_alive(), 'the cancel ends the wait'
    assert rows(db) == []


def test_an_evaluation_that_fails_for_infra_many_times_waits_at_the_cap_and_never_crashes(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """60 infra errors in a row: ``infra_retry * 2**failures`` passed what ``timedelta`` holds at the 58th of 1 ms."""
    wait = EvaluationSettings(infra_retry=timedelta(milliseconds=1), infra_retry_max=timedelta(milliseconds=4))
    ex = queued_executor(db, runtime, tmp_path, 1, wait)
    failed = [InfraFailed(evaluation=0, error='HTTP 429')] * 60
    request = ex._request  # 60 real infra errors would take 60 evaluations: the 61st request is real
    mocker.patch.object(ex, '_request', side_effect=lambda *a: failed.pop() if failed else request(*a))
    events = mocker.spy(Tracer, 'event')
    base = baseline(db, campaign.id).id
    with db.session() as s:
        before = len(s.get_one(Candidate, base).measurements)
    ex.remeasure(base)
    retries = [c.kwargs['retry'] for c in events.call_args_list if c.args[1] == 'evaluation.infra_error']
    assert retries == [0.001, 0.002] + [0.004] * 58
    with db.session() as s:
        [after] = s.get_one(Candidate, base).measurements[before:]
        assert after.error is None, 'the infra errors are no crash: the next try measures it'


INFRA = "printf '%s\\n' '{}' >> \"$PILOOP_RESULT_FILE\"; exit 1".format(
    json.dumps({'status': 'infra_error', 'feedback': 'this evaluator is retired'})
)
"""What an evaluator that its campaign replaced reports, as its adapter refuses to run: an infra error."""


def logged(tmp_path: Path, name: str, then: str) -> list[str]:
    """An evaluate command that logs each try in ``name``, then runs ``then``."""
    return ['sh', '-c', f'echo x >> {tmp_path / name}; {then}']


def logged_tries(tmp_path: Path, name: str) -> int:
    path = tmp_path / name
    return len(path.read_text().split()) if path.exists() else 0


def new_evaluator(db: Database, campaign: Campaign, **overrides: object) -> EvaluatorId:
    """Change the config as ``piloop campaign sync`` does: the config, then the evaluator that it describes is the
    current one."""
    set_config(db, campaign, **overrides)
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, proj, proj.config).id
        s.commit()
        return evaluator


SHORT_RETRY = EvaluationSettings(infra_retry=timedelta(milliseconds=50), infra_retry_max=timedelta(milliseconds=100))


def test_an_infra_retry_ends_once_a_new_evaluator_replaces_its_own_and_the_new_one_measures_next(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The daemon takes the evaluation that waits to run again from the queue at once, not after its wait, and never
    runs the old evaluator again. The new one then measures the baseline (``due``), and the runs start."""
    new_evaluator(db, campaign, evaluate=logged(tmp_path, 'old', INFRA))
    base = baseline(db, campaign.id).id
    with db.session() as s:
        before = len(s.get_one(Candidate, base).measurements)
    scheduler = make_scheduler(db, runtime, tmp_path)  # an infra error waits a minute: longer than any wait here
    run_all(scheduler)
    wait_until(lambda: rows(db) == [(base, False, None)] and logged_tries(tmp_path, 'old') == 1, 'the wait to retry')
    new = new_evaluator(db, campaign, evaluate=logged(tmp_path, 'new', SCORE))

    def settled_by_new() -> bool:
        run_all(scheduler)
        with db.session() as s:
            done = any(m.evaluator_id == new for m in s.get_one(Candidate, base).measurements)
            settled = done and not due(s, s.get_one(Campaign, campaign.id))
        return settled and rows(db) == []

    wait_until(settled_by_new, 'the new evaluator to measure the baseline')
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    assert logged_tries(tmp_path, 'old') == 1, 'the old evaluator never ran again'
    with db.session() as s:
        after = s.get_one(Candidate, base).measurements[before:]
        assert {(m.evaluator_id, m.error) for m in after} == {(new, None)}, 'none under the old one'


def test_the_queue_names_each_evaluation_s_evaluator_and_one_of_a_retired_evaluator_is_no_backlog(
    db: Database, campaign: Campaign
) -> None:
    """A waiting evaluation of a retired evaluator gets no slot, counts for no worker, and leaves the queue
    (``withdraw``). A running one finishes under its own evaluator, and the queue marks it retired."""
    old = new_evaluator(db, campaign, evaluate=['echo', 'old'])
    base = baseline(db, campaign.id).id
    with db.session() as s:
        waiting, _ = queue.request(s, base, old)
        s.commit()
        assert [e.id for e in queue.waiting(s)] == [waiting.id]
        assert KIND.waiting(s, [PROCESS])
        [listed] = queue.view(s).evaluations
        assert (listed.candidate, listed.evaluator, listed.retired) == (base, old, False)
    new = new_evaluator(db, campaign, evaluate=['echo', 'new'])
    with db.session() as s:
        assert queue.waiting(s) == [], 'no slot for it'
        assert not KIND.waiting(s, [PROCESS]), 'no worker counts it as waiting work'
        assert queue.withdraw(s) == 1
        s.commit()
    assert rows(db) == []
    with db.session() as s:
        running, _ = queue.request(s, base, old)
        running.started_at = utcnow()
        queue.request(s, base, new)
        s.commit()
        assert queue.withdraw(s) == 0, 'a running evaluation finishes under its own evaluator'
        shown = [(e.evaluator, e.retired, e.state.kind) for e in queue.view(s).evaluations]
    assert shown == [(old, True, 'running'), (new, False, 'waiting')]


def test_a_sealed_split_s_evaluator_is_retired_only_by_a_new_sealed_command(db: Database, campaign: Campaign) -> None:
    new_evaluator(db, campaign, evaluator={'sealed': ['echo', 'sealed']})
    with db.session() as s:
        proj = s.get_one(Campaign, campaign.id)
        hidden = sealed_evaluator(s, proj, proj.config)
        assert hidden is not None
        s.commit()
        assert not retired(s, hidden.id)
    new_evaluator(db, campaign, evaluate=['echo', 'visible'], evaluator={'sealed': ['echo', 'sealed']})
    with db.session() as s:
        assert not retired(s, hidden.id), 'a new visible evaluator leaves the sealed split as it is'
    store_config(db, campaign.id, {})
    with db.session() as s:
        assert not retired(s, hidden.id), 'a stored config that does not validate names no command that replaced it'
    new_evaluator(db, campaign, evaluator={'sealed': ['echo', 'sealed', 'again']})
    with db.session() as s:
        assert retired(s, hidden.id)


def test_an_evaluate_that_crashes_on_a_signal_of_its_own_is_not_run_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A segfault is the candidate's fault, and a measurement of it."""
    set_config(db, campaign, evaluate=['sh', '-c', f'ulimit -c 0; {killed_first(tmp_path, 1, "SEGV")[2]}'])
    base = baseline(db, campaign.id).id
    queued_executor(db, runtime, tmp_path, slots=1).remeasure(base)
    assert tries(tmp_path) == 1
    assert errors(db, base)[0][-1] == 'evaluate killed by SIGSEGV (exit -11), not by its own exit: '


def test_a_requested_remeasure_is_listed_as_waiting_before_the_daemon_queues_it(
    db: Database, campaign: Campaign
) -> None:
    """The daemon queues one remeasure per campaign at a time, so the others would be invisible while they wait."""
    first = baseline(db, campaign.id).id
    with db.session() as s:
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        base = s.get_one(Candidate, first)
        assert queue.requested(s) == []
        request_remeasure(s, base, System.HUMAN)
        s.commit()
        assert [c.id for c, _ in queue.requested(s)] == [base.id], 'listed from the moment it is accepted'
        evaluation, _ = queue.request(s, base.id, evaluator)
        assert queue.requested(s) == [], 'once queued, it is listed as its evaluation, not twice'
        evaluation.started_at = utcnow()  # a worker took it
        queue.cancel(s, base.id, System.HUMAN)
        request_remeasure(s, base, System.HUMAN)
        s.commit()
        assert [c.id for c, _ in queue.requested(s)] == [base.id], 'a cancelled evaluation does not answer it'
        s.delete(evaluation)
        s.flush()
        assert queue.cancel(s, base.id, System.ORCHESTRATOR) == 1, 'a request that is not queued yet can be cancelled'
        s.commit()
        assert queue.requested(s) == []
        assert queue.cancel(s, base.id, System.ORCHESTRATOR) == 0, 'nothing left to cancel'


def test_a_waiting_evaluation_that_this_daemon_did_not_ask_for_is_never_submitted(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A row that another daemon left has no job here: it waits for the reap, and holds back no request of this one."""
    base = baseline(db, campaign.id)
    with db.session() as s:
        other = Candidate(campaign_id=campaign.id, tree_sha='t2', revision=base.revision, parent_id=base.id)
        s.add(other)
        s.flush()
        s.add(Evaluation(candidate_id=other.id, evaluator_id=s.get_one(Campaign, campaign.id).evaluator_id))
        s.commit()
        other_id = other.id
    ex = queued_executor(db, runtime, tmp_path, slots=2)  # a free slot that the foreign row does not take
    ex.remeasure(base.id)
    assert rows(db) == [(other_id, False, None)], 'measured, and the foreign row still waits'
