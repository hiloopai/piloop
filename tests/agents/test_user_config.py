"""An agent ignores the user's own setup of its harness, unless the campaign loads it, and keeps the user's login."""

import json
from pathlib import Path

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.otel import Tracer
from piloop.plugins.executor.contract import JobSecrets
from piloop.plugins.executor.kit import jobs
from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.contract import PROMPT, USER_CONFIG, Harness, UserConfig, each_provider
from piloop.plugins.runtime.contract import AgentSpec
from tests.conftest import events
from tests.test_lease import agent_job

ISOLATION = {
    'claude': ['--setting-sources', 'project'],
    'codex': ['-c', 'features.hooks=false', '-c', 'notify=[]', '-c', 'developer_instructions=""'],
    'pi': ['--no-skills', '--no-context-files'],
}
SETTINGS = {
    'apiKeyHelper': '/usr/local/bin/team-key',
    'env': {'ANTHROPIC_BASE_URL': 'https://gw.example.com', 'ANTHROPIC_AUTH_TOKEN': 'tok-123', 'MY_TOOL': 'x'},
    'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'notify-me'}]}]},
    'enabledPlugins': {'slack@official': True},
}


def within(flags: list[str], argv: list[str]) -> bool:
    return any(argv[i : i + len(flags)] == flags for i in range(len(argv)))


def test_each_harness_ignores_the_user_s_setup_unless_the_campaign_loads_it() -> None:
    for name, flags in ISOLATION.items():
        default = each_provider()[name]
        loads = type(default).model_validate({'user_config': 'load'})
        assert default.user_config is UserConfig.IGNORE, name
        for argv in (default.headless, default.resume):
            ignored, loaded = default.expanded(argv), loads.expanded(argv)
            assert within(flags, ignored) and USER_CONFIG not in ignored, name
            assert not within(flags, loaded) and len(loaded) == len(ignored) - len(flags), name
    pi = type(each_provider()['pi']).model_validate({'user_config': 'load'})
    assert '--no-extensions' in pi.expanded(pi.headless), 'piloop pins the extension that pi agents need'
    assert Harness(executable='x', headless=[PROMPT]).expanded([USER_CONFIG, PROMPT]) == [PROMPT], 'no flags known'


def claude_at(home: Path, settings: object, **keys: object) -> Claude:
    (home / 'settings.json').write_text(json.dumps(settings))
    return Claude.model_validate({'home': str(home), **keys})


def test_an_isolated_claude_agent_takes_only_the_login_of_the_user_s_settings(tmp_path: Path) -> None:
    claude = claude_at(tmp_path, SETTINGS)
    args, resume = claude.expanded(claude.headless), claude.expanded(claude.resume)
    carried = claude.carried(args, resume)
    assert carried is not None
    assert carried.names == ('apiKeyHelper', 'env.ANTHROPIC_BASE_URL', 'env.ANTHROPIC_AUTH_TOKEN')
    assert carried.env == {'ANTHROPIC_BASE_URL': 'https://gw.example.com', 'ANTHROPIC_AUTH_TOKEN': 'tok-123'}
    for before, after in ((args, carried.args), (resume, carried.resume_args)):
        mine = json.loads(before[before.index('--settings') + 1])
        assert json.loads(after[after.index('--settings') + 1]) == {'apiKeyHelper': '/usr/local/bin/team-key', **mine}
        assert 'tok-123' not in ' '.join(after), 'a token goes into the environment, never into the argv'
        assert within(ISOLATION['claude'], after)

    bare = Claude.model_validate({'home': str(tmp_path), 'headless': [USER_CONFIG, '-p', PROMPT], 'resume': []})
    found = bare.carried(bare.expanded(bare.headless), [])
    assert found is not None
    assert found.args[:2] == ['--settings', '{"apiKeyHelper":"/usr/local/bin/team-key"}'], 'an argv of its own'
    assert found.resume_args == []

    nothing = {
        'the campaign loads the user setup': (SETTINGS, {'user_config': 'load'}),
        'no login keys': ({'model': 'opus', 'env': {'MY_TOOL': 'x'}}, {}),
        'not an object': (['apiKeyHelper'], {}),
        'an argv without {user_config}': (SETTINGS, {'headless': ['-p', PROMPT]}),
    }
    for why, (settings, keys) in nothing.items():
        assert claude_at(tmp_path, settings, **keys).carried(args, resume) is None, why
    (tmp_path / 'settings.json').write_text('{not json')
    assert claude.carried(args, resume) is None, 'Claude ignores a file that it cannot parse'
    (tmp_path / 'settings.json').unlink()
    assert claude.carried(args, resume) is None, 'no settings'


def test_the_worker_gives_the_carried_login_under_piloop_s_own_variables_and_notes_it(
    db: Database, campaign: Campaign, tmp_path: Path, tracer: Tracer, spans: InMemorySpanExporter
) -> None:
    claude = claude_at(tmp_path, SETTINGS)
    job = agent_job(db, campaign, tmp_path).model_copy(update={'harness': claude, 'secrets': {}, 'servers': {}})
    spec = AgentSpec(
        executable='claude',
        args=claude.expanded(claude.headless),
        resume_args=claude.expanded(claude.resume),
        env={'ANTHROPIC_BASE_URL': 'https://campaign-gw.example.com'},
        cwd=tmp_path,
        log_dir=tmp_path,
    )
    with tracer.span('test') as span:
        found = jobs.rendered(job, spec, JobSecrets(values={}, token=SecretStr('t')), span)
    assert found.env['ANTHROPIC_BASE_URL'] == 'https://campaign-gw.example.com', "the harness's env wins"
    assert found.env['ANTHROPIC_AUTH_TOKEN'] == 'tok-123'
    assert 'apiKeyHelper' in found.args[found.args.index('--settings') + 1]
    assert [e['keys'] for e in events(spans, jobs.LOGIN_CARRIED)] == [
        'apiKeyHelper, env.ANTHROPIC_BASE_URL, env.ANTHROPIC_AUTH_TOKEN'
    ]
