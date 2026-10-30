"""Each role's default brief stays within a word budget, so piloop's own text cannot creep back.

The briefs are those of a new campaign, composed by the scheduler and the run controller: the role's prompt, its
guides, its context and its sections, around a campaign brief of two words. Raise a budget only on purpose.
"""

from pathlib import Path

import pytest

from piloop.config import load_campaign_config
from piloop.db.models import AgentSession, Campaign
from piloop.db.session import Database
from piloop.ids import RoleName
from tests.conftest import SETTLE, ScriptRuntime, run_all, write_toml
from tests.daemon.test_roles import recording, scheduler
from tests.daemon.test_run_controller import brief_of, controller, execute, make_run

BUDGETS = {'watchdog': 3300, 'research': 3600, 'orchestrator': 2400}
"""Words of a sweep's whole brief."""
IMPLEMENTER = 1200
"""Words of an implementer's whole brief: a third of it is the run's own context."""


@pytest.mark.parametrize(('role', 'budget'), BUDGETS.items())
def test_a_sweeps_default_brief_stays_within_its_budget(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path, role: str, budget: int
) -> None:
    write_toml(Path(campaign.path), '#!/bin/sh\ntrue\n')
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(
            Path(campaign.path), plugins={'role': {role: {'triggers': []}}}
        )
        s.add(AgentSession(kind=RoleName(role), campaign_id=campaign.id, reason='by hand'))
        s.commit()
    sched = scheduler(db, recording(runtime, []), tmp_path)
    assert run_all(sched) == [1]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    words = len((tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text().split())
    assert words <= budget, f'the {role} brief has {words} words, over its budget of {budget}'


def test_an_implementers_default_brief_stays_within_its_budget(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    run_id = make_run(db, campaign)
    execute(controller(db, runtime, tmp_path), db, run_id)
    words = len(brief_of(db, run_id).split())
    assert words <= IMPLEMENTER, f'the implementer brief has {words} words, over its budget of {IMPLEMENTER}'
