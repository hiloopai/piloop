import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import EnvSettingsSource

from piloop import deprecated, settings as settings_module
from piloop.actor import Agent, System
from piloop.config import CampaignAgents
from piloop.deprecated import PiloopDeprecationWarning
from piloop.plugins.harness.contract import cleared_env, each_provider
from piloop.plugins.host import Scope, campaign_defaults
from piloop.settings import UNDER_TEST_ENV, MovedEnv, Settings, StorageSettings, TracingSettings, env_names, expand_env


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
    monkeypatch.setenv('PILOOP_ACTOR', 'scout#3')
    assert Settings(home=tmp_path).actor == Agent(role='scout', session=3)
    monkeypatch.setenv('PILOOP_ACTOR', 'scout#')
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


def test_the_implementer_s_timings_of_config_toml_are_its_role_s_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``[daemon] startup_relaunches`` and ``[agents] finding_grace`` and ``wrap_up_lead`` are the implementer's."""
    (tmp_path / 'config.toml').write_text(
        '[daemon]\nstartup_relaunches = 4\n[agents]\nfinding_grace = 5\nwrap_up_lead = 120\n'
    )
    monkeypatch.setenv('PILOOP_AGENTS__FINDING_GRACE', 'PT30S')
    with pytest.warns(PiloopDeprecationWarning) as warned:
        home = Settings(home=tmp_path)
    assert [str(w.message).split('; removed')[0] for w in warned] == [
        '`daemon.startup_relaunches` is deprecated; write `plugins.role.implementer.startup_relaunches`',
        '`agents.finding_grace` is deprecated; write `plugins.role.implementer.finding_grace`',
        '`agents.wrap_up_lead` is deprecated; write `plugins.role.implementer.wrap_up_lead`',
        'PILOOP_AGENTS__FINDING_GRACE is deprecated; set PILOOP_PLUGINS__ROLE__IMPLEMENTER__FINDING_GRACE',
    ]
    assert campaign_defaults(home.plugins) == {
        'role': {'implementer': {'startup_relaunches': 4, 'finding_grace': 'PT30S', 'wrap_up_lead': 120}}
    }


def test_a_moved_variable_source_reads_a_field_as_the_environment_does(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('PILOOP_MIGRATION_BACKUPS', '5')
    env = EnvSettingsSource(Settings, env_prefix='PILOOP_')
    moved = MovedEnv(env, deprecated.bridges(Scope.HOME), 'PILOOP_')
    field = Settings.model_fields['migration_backups']
    assert moved.get_field_value(field, 'migration_backups') == env.get_field_value(field, 'migration_backups')
