"""The implementer's default brief stays within a word budget, so piloop's own text cannot creep back.

The brief is that of a new campaign, composed by the run controller: the role's prompt, its guides, its context
and its sections, around a campaign brief of two words. Raise a budget only on purpose.
"""

from pathlib import Path

from piloop.db.models import Campaign
from piloop.db.session import Database
from tests.conftest import ScriptRuntime
from tests.daemon.test_run_controller import brief_of, controller, execute, make_run

IMPLEMENTER = 560
"""Words of an implementer's whole brief. Workers act on few of piloop's lines, and a plain agent does as well on a
prompt of under a hundred words: each line has to earn its place."""


def test_an_implementers_default_brief_stays_within_its_budget(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    words = len(brief_of(db, run_id).split())
    assert words <= IMPLEMENTER, f'the implementer brief has {words} words, over its budget of {IMPLEMENTER}'
