import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from piloop import settings as settings_module
from piloop.actor import Agent, System
from piloop.config import CampaignAgents
from piloop.plugins.harness.contract import cleared_env, each_provider
from piloop.problems import HomeConfigError
from piloop.settings import (
    UNDER_TEST_ENV,
    DaemonSettings,
    Settings,
    StorageSettings,
    TracingSettings,
    auto_api_processes,
    env_names,
    expand_env,
)
from tests.isolation import API_PROCESSES_ENV


def test_the_test_session_runs_in_a_temporary_home() -> None:
    """tests/conftest.py moves HOME and PILOOP_HOME before piloop is imported, so even the import-time ``settings``
    names a temporary home."""
    assert os.environ[UNDER_TEST_ENV] == '1'
    assert Path.home() != settings_module.REAL_HOME
    assert settings_module.settings.home == Path(os.environ['PILOOP_HOME'])
    assert settings_module.settings.home.parent != settings_module.REAL_HOME


def test_each_test_has_a_home_of_its_own_for_every_built_in_harness(tmp_path: Path) -> None:
    for name, harness in each_provider().items():
        assert harness.home is not None and Path(expand_env(harness.home)) == tmp_path / 'harnesses' / name
    assert cleared_env() and not cleared_env() & set(os.environ), 'no harness session that runs the tests reaches them'


def test_e2e_campaigns_get_the_real_harness_homes_without_the_harness_session_of_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.e2e import conftest as e2e

    real = {'PATH': '/bin', 'HOME': '/home/me', 'CLAUDE_CONFIG_DIR': '/home/me/claude', 'CODEX_HOME': '/home/me/codex'}
    caller = real | dict.fromkeys(cleared_env(), '1') | {'PILOOP_HOME': '/home/me/.piloop', 'VIRTUAL_ENV': '/venv'}
    monkeypatch.setattr(e2e, 'CALLER_ENV', caller)
    assert e2e.caller_env(Path('/tmp/home')) == real | {UNDER_TEST_ENV: '1', 'PILOOP_HOME': '/tmp/home'}


def test_the_e2e_install_check_runs_in_a_temporary_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``make e2e`` runs with the user's own ``HOME``, whose ``.piloop`` the guard refuses: the check names another."""
    from tests.e2e import conftest as e2e

    monkeypatch.setitem(e2e.CALLER_ENV, 'HOME', str(settings_module.REAL_HOME))
    (tmp_path / 'bin').mkdir()
    (tmp_path / 'bin' / 'piloop').symlink_to(Path(sys.executable).parent / 'piloop')
    assert e2e.Install(tmp_path, tmp_path / 'bin').version().startswith('piloop ')


def test_under_test_the_home_of_a_real_install_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``~/.piloop`` and its siblings in the user's passwd home directory, which a moved ``$HOME`` does not hide."""
    me = tmp_path / 'me'
    monkeypatch.setattr(settings_module, 'REAL_HOME', me)
    for home in (me / '.piloop', me / '.piloop-new'):
        with pytest.raises(ValidationError, match=f'{home} is the home of a real install'):
            Settings(home=home)
        monkeypatch.setenv('PILOOP_HOME', str(home))
        with pytest.raises(ValidationError, match=f'{home} is the home of a real install'):
            Settings()
    assert Settings(home=me / 'scratch').home == me / 'scratch'
    assert Settings(home=tmp_path / '.piloop').home == tmp_path / '.piloop', 'not in the real home directory'
    monkeypatch.delenv(UNDER_TEST_ENV)
    assert Settings().home == me / '.piloop-new', 'outside a test run it is just the home'


def test_storage_is_read_from_config_toml_under_the_home_and_the_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'config.toml').write_text('[storage]\nmin_free_gb = 30\n')
    assert Settings(home=tmp_path).storage == StorageSettings(min_free_gb=30)
    assert Settings(home=tmp_path / 'elsewhere').storage == StorageSettings(), 'no config.toml there'
    monkeypatch.setenv('PILOOP_HOME', str(tmp_path))
    monkeypatch.setenv('PILOOP_STORAGE__MIN_FREE_GB', '5')
    assert Settings().storage.min_free_gb == 5
    with pytest.raises(ValidationError, match='Extra inputs are not permitted'):
        StorageSettings.model_validate({'storage_class': 'STANDARD'}), 'where blobs go is [plugins.cas]'


def test_an_env_placeholder_is_the_variable_or_its_default_in_the_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('A_DIR', '/a')
    monkeypatch.setenv('EMPTY', '')
    monkeypatch.delenv('UNSET', raising=False)
    glob = '{env:A_DIR:~/.a}/x {env:UNSET:~/.b}/y {env:EMPTY:/c} {env:UNSET}z {cwd}'
    assert expand_env(glob) == f'/a/x {Path.home()}/.b/y /c z {{cwd}}', 'an empty variable is unset; other braces stay'
    assert env_names(glob + ' {env:A_DIR:other}') == ['A_DIR', 'UNSET', 'EMPTY']


def test_a_runtime_value_that_is_no_table_of_tables_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('PILOOP_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('[plugins]\nruntime = 3\n')
    with pytest.raises(ValidationError, match='plugins.runtime'):
        Settings()
    (tmp_path / 'config.toml').write_text('[plugins.runtime]\nnice = 3\n')
    with pytest.raises(ValidationError, match=r'runtime\.nice\n  Value error, a runtime is a table'):
        _ = Settings().providers


def test_the_actor_is_read_from_its_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('PILOOP_ACTOR', 'implementer#12')
    assert Settings(home=tmp_path).actor == Agent(role='implementer', session=12)
    monkeypatch.setenv('PILOOP_ACTOR', 'auditor#3')
    assert Settings(home=tmp_path).actor == Agent(role='auditor', session=3)
    monkeypatch.setenv('PILOOP_ACTOR', 'auditor#')
    with pytest.raises(ValidationError, match=r'actor\.Agent\.session'):
        Settings(home=tmp_path)
    monkeypatch.delenv('PILOOP_ACTOR')
    assert Settings(home=tmp_path).actor == System.HUMAN


def test_slow_write_is_one_ms_read_from_config_toml_and_the_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings(home=tmp_path).tracing.slow_write == timedelta(milliseconds=1)
    (tmp_path / 'config.toml').write_text('[tracing]\nslow_write = 0\n')
    assert Settings(home=tmp_path).tracing.slow_write == timedelta(0)

    monkeypatch.setenv('PILOOP_TRACING__SLOW_WRITE', 'PT0.25S')
    assert Settings(home=tmp_path).tracing.slow_write == timedelta(milliseconds=250)
    with pytest.raises(ValidationError, match='greater than or equal'):
        TracingSettings(slow_write=timedelta(seconds=-1))


def test_the_daemon_s_timings_are_read_from_config_toml_the_environment_wins_and_a_campaign_sets_its_graces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'config.toml').write_text('[daemon]\nfeed_page = 5\n[agents]\nwrap_up_grace = 60\n')
    monkeypatch.setenv('PILOOP_AGENTS__WRAP_UP_GRACE', 'PT30S')
    home = Settings(home=tmp_path)
    assert (home.daemon.feed_page, home.agents.wrap_up_grace) == (5, timedelta(seconds=30))
    campaign = CampaignAgents(wrap_up_grace=timedelta(minutes=20))
    assert campaign.over(home.agents) == home.agents.model_copy(update={'wrap_up_grace': timedelta(minutes=20)})
    assert CampaignAgents().over(home.agents) == home.agents, "unset: the home's"


@pytest.mark.parametrize(
    ('platform', 'cpus', 'processes'),
    [('linux', 32, 8), ('linux', 16, 8), ('linux', 4, 2), ('linux', 3, 1), ('linux', 1, 0), ('linux', None, 0)]
    + [('darwin', 32, 0), ('win32', 32, 0)],
)
def test_auto_api_processes_are_half_the_cores_up_to_8_on_linux_and_none_elsewhere(
    platform: str, cpus: int | None, processes: int
) -> None:
    assert auto_api_processes(platform, cpus) == processes


def test_api_processes_are_auto_by_default_and_a_value_of_config_toml_or_the_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_module.os, 'process_cpu_count', lambda: 6)
    monkeypatch.setattr(settings_module.sys, 'platform', 'linux')
    monkeypatch.delenv(API_PROCESSES_ENV)
    home = Settings(home=tmp_path).daemon
    assert (home.api_processes, home.api_process_count) == ('auto', 3)
    (tmp_path / 'config.toml').write_text('[daemon]\napi_processes = 0\n')
    assert Settings(home=tmp_path).daemon.api_process_count == 0, 'the daemon serves alone'
    monkeypatch.setenv(API_PROCESSES_ENV, '5')
    assert Settings(home=tmp_path).daemon.api_process_count == 5
    monkeypatch.setenv(API_PROCESSES_ENV, 'auto')
    assert Settings(home=tmp_path).daemon.api_process_count == 3
    with pytest.raises(ValidationError, match='greater than or equal to 0'):
        DaemonSettings.model_validate({'api_processes': -1})


def test_a_key_of_config_toml_that_0_8_removed_fails_with_one_line_that_names_it_and_its_line(tmp_path: Path) -> None:
    (tmp_path / 'config.toml').write_text('[agents]\nattach_lines = 10\nwake_budget = 800\n')
    with pytest.raises(HomeConfigError) as refused:
        Settings(home=tmp_path)
    assert str(refused.value) == (
        f'{tmp_path / "config.toml"}:3: delete [agents] wake_budget: piloop 0.8.0 removed it (`piloop wake` is'
        ' removed). See https://piloop.autoresearch.inc/guides/upgrading'
    )


def test_a_config_toml_that_does_not_load_is_one_message_for_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('PILOOP_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('[agents\n')
    with pytest.raises(HomeConfigError, match=r'config\.toml is not valid TOML: '):
        settings_module.load()
    (tmp_path / 'config.toml').write_text('[agents]\nattach_line = 10\n')
    with pytest.raises(HomeConfigError) as refused:
        settings_module.load()
    assert str(refused.value).splitlines()[1].startswith('  agents.attach_line = 10: Extra inputs are not permitted.')
