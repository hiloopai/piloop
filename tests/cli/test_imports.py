"""The commands start fast: those that call the API load no database, server, git or TUI, and the commands that an
agent's relayed call runs on the server, which open the database, load no server and no campaign code."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from covalence import Cid

from piloop.proc import PILOOP_ARGV
from tests.conftest import at_head

HEAVY = ('sqlalchemy', 'alembic', 'git', 'textual', 'fastapi', 'uvicorn', 'covalence')
WEB = ('fastapi', 'starlette', 'uvicorn', 'textual', 'piloop.net.server')
"""The server's stack and the TUI, which no relayed call needs."""
CAMPAIGNS = (*WEB, 'git', 'piloop.campaign.campaigns')
"""What a relayed call that reads only the database and the CAS does not need: also git and the campaigns' code."""


@pytest.mark.parametrize(
    'module',
    [
        'main',
        'views',
        'control',
        'inbox',
        'piloop.plugins.campaign_type.kit.backlog.cli',
        'piloop.plugins.campaign_type.optimization.cli',
        'piloop.plugins.campaign_type.kit.evaluation.cli',
        'piloop.plugins.campaign_type.kit.runs.cli',
        'limits',
        'token',
        'secret',
    ],
)
def test_a_client_command_loads_no_heavy_dependency(module: str) -> None:
    dotted = module if '.' in module else f'piloop.cli.{module}'
    probe = f'import sys, {dotted}; print(*sorted(m for m in {HEAVY!r} if m in sys.modules))'
    loaded = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True).stdout
    assert loaded.split() == []


@pytest.mark.parametrize(
    ('argv', 'said', 'unneeded'),
    [
        (['sql', 'SELECT 1 AS one'], 'one', CAMPAIGNS),
        (['sql', '-'], 'one', CAMPAIGNS),
        (['cas', 'get', str(Cid.of(b'never put'))], 'the CAS has no blob', CAMPAIGNS),
        (['schema', 'queries'], '## ', WEB),  # the guides of every plugin, and the vcs whose commands they show
    ],
    ids=['sql', 'sql -', 'cas get', 'schema'],
)
def test_a_relayed_call_on_the_server_loads_only_what_it_needs(
    argv: list[str], said: str, unneeded: tuple[str, ...], schema: Path, tmp_path: Path
) -> None:
    """Each relayed call is a process of its own on the server (``Runner``): its imports are most of its cost."""
    at_head(schema, tmp_path / 'piloop.db')
    probe = (
        f'import sys; sys.argv = ["piloop", *{argv!r}]\n'
        'from piloop.cli.main import main\n'
        'try:\n    main()\nexcept SystemExit as e:\n    print(e.code)\n'
        f'print("loaded:", *sorted(m for m in {unneeded!r} if m in sys.modules))'
    )
    env = os.environ | {'PILOOP_HOME': str(tmp_path)}
    done = subprocess.run(
        [PILOOP_ARGV[0], '-P', '-c', probe],
        input='SELECT 1 AS one',
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    *printed, loaded = done.stdout.splitlines()
    assert said in '\n'.join(printed), 'the command ran'
    assert loaded.split() == ['loaded:']
