"""An implementer that works past its time is told to wrap up in its turn, and finishes before its timeout.

    uv run pytest -m e2e tests/e2e/test_deadline.py -rA [--e2e-harness claude] [--e2e-model <model>]

The role timeout is 8 minutes, so the wrap-up comes 2 minutes before it: room for a commit, a submission and its
verdict, and the run's end. The idea asks for more half-minute steps than fit, all in one turn. Claude Code refuses a
long ``sleep``, so a step sleeps in Python. The campaign starts paused, with the idea on its board, so the one worker
claims it. A Claude agent reads the wrap-up after its next tool call, commits, submits and ends its run, and the daemon
ends it a minute later. About 9 minutes and 0.15 USD.
"""

from collections.abc import Iterator

import pytest
from covalence import Cid

from piloop.agents.roles import read_brief
from piloop.plugins.cas.contract import CAS
from piloop.plugins.runtime.contract import TAG_ENV
from piloop.plugins.runtime.kit.tags import tagged
from tests.conftest import wait_until
from tests.e2e.campaign import CLAIM_THE_BOARD, OPEN, Smoke, needs
from tests.e2e.conftest import MakeCampaign

pytestmark = pytest.mark.e2e

TIMEOUT = 480
CONFIG = {
    'plugins': {
        'role': {
            'implementer': {'max_concurrent': 1, 'timeout': TIMEOUT},
            'research': {'triggers': []},
            'watchdog': {'triggers': []},
        },
        'stop_rule': {'max_runs': 3, 'max_hours': 1, 'max_tokens': 5_000_000},
    },
}
PLAN = (
    'This idea tests the time limit, not the compressor. Do twenty steps in this one turn. Each step is one Bash tool'
    ' call of its own: `python3 -c "import time; time.sleep(30)" && echo "step n" >> workspace/steps.txt`. Never put'
    ' two steps in one command, and never run a step in the background. Change and commit nothing until a [piloop]'
    ' message tells you to wrap up. Then set the zlib level in compressor.py to 9, commit, run `piloop submit`, and'
    ' end your run with `piloop run end submitted`.'
)


@pytest.fixture(scope='module')
def campaign(make_campaign: MakeCampaign) -> Iterator[Smoke]:
    yield make_campaign(config=CONFIG, paused='e2e: the idea goes on the board first', brief=CLAIM_THE_BOARD)


def test_the_brief_states_the_deadline(campaign: Smoke) -> None:
    campaign.add_idea('long', 'Work past the time limit', PLAN, 0)
    campaign.piloop('resume', campaign.name)
    wait_until(lambda: campaign.run_in('long', {'running'}) is not None, timeout=120)
    run = campaign.run_of('long')
    assert run is not None
    brief = read_brief(CAS.selected({}).open(campaign.home), Cid(run['brief_cid']))  # a worker elsewhere has brief.md
    assert '- Your session ends 8 minutes after it starts, at `$PILOOP_DEADLINE`' in brief


@needs(test_the_brief_states_the_deadline)
def test_the_wrap_up_reaches_the_turn_and_the_run_finishes_before_its_timeout(campaign: Smoke) -> None:
    wait_until(lambda: campaign.run_in('long', OPEN, within=False) is not None, timeout=TIMEOUT + 120)
    run = campaign.run_of('long')
    assert run is not None
    [implementer] = campaign.sql(
        "SELECT * FROM agent_sessions WHERE subject_kind = 'run' AND subject_id = ?", run['id']
    )
    delivered = campaign.sql(
        "SELECT kind FROM events WHERE target_kind = 'agent_session' AND target_id = ? AND kind LIKE 'comment.%'",
        implementer['id'],
    )
    assert [e['kind'] for e in delivered] == ['comment.queued', 'comment.delivered'], 'the wrap-up note, read'
    assert (run['status'], run['end']) == ('succeeded', 'finished'), run['error']
    ended = campaign.sql(
        "SELECT json_extract(payload, '$.reason') AS reason FROM events WHERE kind = 'agent_session.stop_ended'"
        ' AND target_id = ?',
        implementer['id'],
    )
    assert [e['reason'] for e in ended] in ([], ['ended its run']), 'it ended by itself, or after its run end'
    queue = campaign.home / 'runs' / str(run['id']) / 'queued-messages'
    assert not queue.exists(), 'taken in the turn, not left for a turn of its own'
    assert 'timeout' not in (implementer['error'] or ''), implementer['error']
    assert [p for t in campaign.process_tags(run['id']) for p in tagged(f'{TAG_ENV}={t}')] == []
    print(
        f'run {run["id"]} {run["end"]} in {implementer["ended_at"]} - {implementer["started_at"]}; ended {[e["reason"] for e in ended]}'
    )
