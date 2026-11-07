"""Every command that looks at an agent asks the agent's runtime, through the ``Runtime`` methods, never its name."""

from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from piloop.cli import agent_io, common
from piloop.db.models import AgentSession, AgentSessionState, Campaign, utcnow
from piloop.db.session import Database
from piloop.plugins.runtime.contract import RUNTIMES
from piloop.settings import Settings
from tests.conftest import AUDITOR, PLUGIN, ROLE_GROUP, forget_loaded, install_plugins, reconfigured
from tests.reports.test_overview import overview

SHOWN = [EntryPoint('shown', f'{PLUGIN}:Shown', RUNTIMES.group), EntryPoint(AUDITOR, f'{PLUGIN}:Auditor', ROLE_GROUP)]


@pytest.fixture
def shown(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Install a package that adds the ``shown`` runtime (``tests.fixtures.plugin.Shown``) and the ``auditor`` role."""
    install_plugins(monkeypatch, SHOWN)
    yield
    forget_loaded()


@pytest.mark.usefixtures('shown')
def test_attach_and_status_read_the_output_through_a_runtime_that_a_package_adds(
    db: Database, campaign: Campaign, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = reconfigured(campaign.config, plugins={'runtime': {'use': 'shown'}})
        agent_session = AgentSession(
            kind=AUDITOR,
            campaign_id=campaign.id,
            harness='script',
            model='m',
            runtime='shown',
            handle={'pid': 7, 'start_time': 0, 'log_dir': str(tmp_path)},
            state=AgentSessionState.RUNNING,
            log_dir=str(tmp_path),
        )
        s.add(agent_session)
        s.commit()
        row = s.get_one(Campaign, campaign.id)
        assert ', on ' in (text := overview(s, row, utcnow())) and ': pane 7' in text
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path))
    printed: list[object] = []
    monkeypatch.setattr(common.console, 'print', lambda line, **_: printed.append(line))
    agent_io.attach(agent_session.id)
    assert printed == ['pane 7']
