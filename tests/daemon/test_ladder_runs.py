"""The evaluation ladder: a run's own version climbs it and may stop at a rung; everything else climbs every rung.

Everything else is a baseline, the best, a reference, a requested remeasure, and the evaluations that confirm a
candidate.
"""

from pathlib import Path

import pytest
from inline_snapshot import snapshot
from pytest_mock import MockerFixture

from piloop.actor import System
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue
from piloop.plugins.campaign_type.kit.evaluation.measure import request_remeasure, requested
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from tests.conftest import ScriptRuntime, baseline, scoring, wait_until
from tests.daemon.test_evaluation_queue import evaluated, in_thread, queued_executor, rows, run_candidates
from tests.daemon.test_run_controller import controller, execute, make_run, set_config, told_of

CLIMB = (
    'n=$(wc -l < solve.txt); echo climb >> {log};'
    ' printf \'{{"rung": "screen", "metrics": {{"score": %d, "cost": 1}}}}\\n\' $((3 - n)) >> "$PILOOP_RESULT_FILE";'
    ' printf \'{{"status": "ok", "score": %d, "direction": "higher", "metrics": {{"cost": 2}}}}\\n\' $n'
    ' >> "$PILOOP_RESULT_FILE"'
)
"""An evaluate that logs each climb to ``log``, and reports a screen that disagrees with the full panel: a line more
scores 1 less at the screen, and 1 more in full."""
SCORE = {'name': 'score', 'direction': 'higher', 'role': 'primary'}
COST = {'name': 'cost', 'direction': 'lower'}
SCREEN = {'name': 'screen', 'stop': {'score': {'margin': 0.5}}}


def laddered(db: Database, campaign: Campaign, tmp_path: Path, *, hold: bool = False, **overrides: object) -> Path:
    """Give the campaign the ``CLIMB`` evaluate and a screen, held while ``tmp_path/hold`` exists. Return its log."""
    log = tmp_path / 'climbs'
    held = f'git rev-parse HEAD >> {tmp_path / "evaluated"}; while [ -f {tmp_path / "hold"} ]; do sleep 0.05; done; '
    script = (held if hold else '') + CLIMB.format(log=log)
    set_config(
        db, campaign, **{'evaluate': ['sh', '-c', script], 'metrics': [SCORE, COST], 'ladder': [SCREEN]} | overrides
    )
    return log


def climbs(log: Path) -> int:
    return len(log.read_text().split()) if log.exists() else 0


def rungs_of(db: Database, candidate_id: CandidateId) -> list[tuple[str | None, str | None, dict[str, float]]]:
    with db.session() as s:
        return [(m.rung, m.stopped, m.metrics) for m in s.get_one(Candidate, candidate_id).measurements]


@pytest.mark.usefixtures('submitting')
def test_a_run_stops_its_version_at_a_rung_and_a_remeasure_climbs_in_full(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The run's version stops at the screen against the best's value there; what someone asks for climbs in full."""
    log = laddered(db, campaign, tmp_path)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)  # the new evaluator measures the baseline first, as the daemon does
    assert rungs_of(db, baseline(db, campaign.id).id)[-2:] == [
        ('screen', None, {'score': 2.0, 'cost': 1.0}),
        (None, None, {'score': 1.0, 'cost': 2.0}),
    ], 'a reference climbs every rung, whatever it measures there'
    run_id = make_run(db, campaign)
    with scoring(ex, db):
        execute(ex, db, run_id)
    assert climbs(log) == 2, 'the baseline, then one climb that stopped'
    [c] = run_candidates(db, run_id)
    assert rungs_of(db, c.id) == [('screen', 'score 1 vs 2 (-1; margin 0.5)', {'score': 1.0, 'cost': 1.0})]
    with db.session() as s:
        assert told_of(s, c.id) == 'stopped at a rung of the ladder: score 1 vs 2 (-1; margin 0.5)'
        request_remeasure(s, s.get_one(Candidate, c.id), System.HUMAN, repeats=1)
        s.commit()
    ex.remeasure(c.id)
    assert [(rung, stopped) for rung, stopped, _ in rungs_of(db, c.id)][1:] == [('screen', None), (None, None)], (
        'what someone asked for climbs every rung'
    )
    with db.session() as s:
        assert told_of(s, c.id) == snapshot(
            'score 2 over 1 eval: the best of the score track now, rank 1 of 2; vs parent +1'
        )
        assert best_of(s, s.get_one(Campaign, campaign.id)) == s.get_one(Candidate, c.id)
        assert requested(s, s.get_one(Campaign, campaign.id)) == []


@pytest.mark.usefixtures('submitting')
def test_a_rung_with_its_own_command_runs_first_and_its_stop_spares_the_evaluate_command(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    smoke = {
        'name': 'smoke',
        'evaluate': ['sh', '-c', 'echo \'{"metrics": {"score": 0, "cost": 0.5}}\' >> "$PILOOP_RESULT_FILE"'],
        'stop': {'score': {'floor': 1}},
    }
    log = laddered(db, campaign, tmp_path)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    laddered(db, campaign, tmp_path, ladder=[smoke, SCREEN])
    ex.remeasure(baseline(db, campaign.id).id)
    assert climbs(log) == 2, 'a reference runs past a stop that holds'
    run_id = make_run(db, campaign)
    with scoring(ex, db):
        execute(ex, db, run_id)
    [c] = run_candidates(db, run_id)
    assert climbs(log) == 2, 'the smoke stopped the climb: the evaluate command never ran'
    assert rungs_of(db, c.id) == [('smoke', 'score 0 past its floor 1', {'score': 0.0, 'cost': 0.5})]


@pytest.mark.usefixtures('submitting')
def test_a_remeasure_that_joins_a_stopped_climb_is_asked_again_and_then_climbs_in_full(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A remeasure that joins the climb of a submission gets its stop, which leaves the request open, so the daemon
    asks again, and that climb goes to the full panel."""
    laddered(db, campaign, tmp_path, hold=True)
    ex = queued_executor(db, runtime, tmp_path, slots=2)
    ex.remeasure(baseline(db, campaign.id).id)
    (tmp_path / 'hold').touch()
    waits = mocker.spy(queue, 'request')
    run_id = make_run(db, campaign)
    with scoring(ex, db):
        ran = in_thread(lambda: execute(ex, db, run_id))
        wait_until(lambda: len(evaluated(tmp_path)) == 2, 'the climb of the submission')
        [c] = run_candidates(db, run_id)
        with db.session() as s:
            request_remeasure(s, s.get_one(Candidate, c.id), System.HUMAN, repeats=1)
            s.commit()
        joined = in_thread(lambda: ex.remeasure(c.id))
        wait_until(lambda: waits.call_count == 2, 'the remeasure joins')
        (tmp_path / 'hold').unlink()
        ran.join(30)
        joined.join(30)
    with db.session() as s:
        assert [m.rung for m in s.get_one(Candidate, c.id).measurements] == ['screen']
        assert told_of(s, c.id) == 'stopped at a rung of the ladder: score 1 vs 2 (-1; margin 0.5)'
        assert [r.id for r, _ in requested(s, s.get_one(Campaign, campaign.id))] == [c.id], 'still asked'
    assert len(evaluated(tmp_path)) == 2, 'the baseline, then the tree once'
    ex.remeasure(c.id)
    with db.session() as s:
        assert [m.rung for m in s.get_one(Candidate, c.id).measurements][-2:] == ['screen', None]
        assert requested(s, s.get_one(Campaign, campaign.id)) == []
    assert rows(db) == []
