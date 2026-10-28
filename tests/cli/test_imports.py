"""The commands that call the API start fast: their modules load no database, server, git or TUI."""

import subprocess
import sys

import pytest

HEAVY = ('sqlalchemy', 'alembic', 'git', 'textual', 'fastapi', 'uvicorn', 'covalence')


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


def test_the_journal_command_loads_no_heavy_dependency() -> None:
    probe = f'import sys, piloop.plugins.memory.local.cli; print(*sorted(m for m in {HEAVY!r} if m in sys.modules))'
    loaded = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, check=True).stdout
    assert loaded.split() == []
