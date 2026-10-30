"""An agent ignores the user's own setup of its harness, unless the campaign loads it, and keeps the user's login."""

import json
from pathlib import Path

from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.contract import PROMPT, USER_CONFIG, Harness, UserConfig, each_provider

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
