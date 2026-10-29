"""The orchestrator skills: composed from the parts that the installed packages declare, written into the skills
directory of each harness on PATH, with drift reported."""

import os
import re
from collections.abc import Iterator
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from rich.console import Console

from piloop.cli import common, install, main as cli, skills
from piloop.cli.main import COMMANDS
from piloop.cli.skills import SKILLS, SkillPart
from piloop.plugins.host import PluginError
from piloop.settings import Settings
from tests.daemon.test_roles import forget_loaded, installed


@pytest.fixture
def harness_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude'))
    settings = Settings(home=tmp_path / 'piloop', agent_skills_dir=tmp_path / 'agents/skills')
    monkeypatch.setattr(common, 'settings', settings)
    monkeypatch.setattr(common, 'console', Console(record=True, width=300, force_terminal=False))
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    (tmp_path / 'bin').mkdir()
    return settings


def on_path(*names: str) -> None:
    for name in names:
        exe = Path(os.environ['PATH']) / name
        exe.write_text('#!/bin/sh\n')
        exe.chmod(0o755)


def printed() -> str:
    return common.console.export_text(clear=True)


ROOT = Path(__file__).parent.parent.parent
COMMAND = re.compile(r'\bpiloop ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?')
"""``piloop <command> [<subcommand>]`` as a skill or AGENTS.md writes it."""


def test_every_skill_is_an_agent_skill_named_after_its_directory() -> None:
    shipped = skills.composed()
    assert list(shipped) == ['piloop-operate', 'piloop-review', 'piloop-setup']
    for name, text in shipped.items():
        front = re.match(r'---\nname: (.+)\ndescription: (.+)\n---\n', text)
        assert front is not None and front[1] == name and len(front[2]) <= 1024, 'the frontmatter every harness reads'
        assert len(text.splitlines()) <= 500, 'the Agent Skills soft cap on a SKILL.md'


def test_every_piloop_command_the_skills_name_exists() -> None:
    """A renamed or deleted command fails here, not in the shell of an orchestrator."""
    texts = [*skills.composed().values(), (ROOT / 'AGENTS.md').read_text()]
    code = [part for text in texts for part in text.split('`')[1::2]]  # inline spans and fenced blocks
    named = {m.groups() for part in code for m in COMMAND.finditer(part)}
    missing = sorted(
        f'piloop {" ".join(filter(None, (command, sub)))}'
        for command, sub in named
        if command not in cli.app or (sub and _subcommands(command) and sub not in cli.app[command])
    )
    assert missing == []
    assert ('inbox', 'wait') in named, 'the scan reads the skills at all'


def _subcommands(command: str) -> set[str]:
    return {name for name in cli.app[command] if not name.startswith('-')}


CAMPAIGN_TYPES = 'piloop.plugins.campaign_type.'


def test_the_core_parts_name_no_command_of_a_campaign_type() -> None:
    """Ideas, candidates, runs and verdicts are the optimization campaign type's: its own parts teach them."""
    typed = {name for name, path in COMMANDS.paths().items() if path.startswith(CAMPAIGN_TYPES)}
    parts, paths = SKILLS.loaded(), SKILLS.paths()
    core = [part.text.read_text() for name, part in parts.items() if not paths[name].startswith(CAMPAIGN_TYPES)]
    named = {m[1] for text in core for part in text.split('`')[1::2] for m in COMMAND.finditer(part)}
    assert {'idea', 'candidate', 'run', 'history'} <= typed
    assert named & typed == set()
    assert 'inbox' in named, 'the scan reads the core parts at all'


TRIAGE = SkillPart('piloop-operate', 350, ROOT / 'tests' / 'fixtures' / 'skills' / 'triage.md')
"""A package's part of the operate skill, right after its escalations."""
REVIEW = SkillPart('piloop-review', 0, ROOT / 'tests' / 'fixtures' / 'skills' / 'review.md', replaces=('review',))
"""A package's review skill, in place of piloop's."""


@pytest.fixture
def forgets() -> Iterator[None]:
    yield
    forget_loaded()


def test_a_package_adds_a_part_to_a_skill_in_its_order_and_replaces_another(
    monkeypatch: pytest.MonkeyPatch, forgets: None
) -> None:
    installed(
        monkeypatch,
        [
            EntryPoint('triage', f'{__name__}:TRIAGE', SKILLS.group),
            EntryPoint('their-review', f'{__name__}:REVIEW', SKILLS.group),
        ],
    )
    shipped = skills.composed()
    operate = shipped['piloop-operate']
    assert operate.index('### Escalations') < operate.index('## Triage') < operate.index('### Messages')
    assert shipped['piloop-review'] == REVIEW.text.read_text(), 'its part replaces the whole skill'


def test_two_parts_that_replace_one_and_a_part_of_no_piloop_skill_are_refused(
    monkeypatch: pytest.MonkeyPatch, forgets: None
) -> None:
    installed(
        monkeypatch,
        [
            EntryPoint('their-review', f'{__name__}:REVIEW', SKILLS.group),
            EntryPoint('other-review', f'{__name__}:REVIEW', SKILLS.group),
        ],
    )
    with pytest.raises(PluginError, match="other-review and their-review both replace 'review'"):
        skills.composed()
    installed(monkeypatch, [EntryPoint('mine', f'{__name__}:MINE', SKILLS.group)])
    with pytest.raises(PluginError, match="mine = .* in piloop.skills: 'mine' is no skill of piloop"):
        skills.composed()


MINE = SkillPart('mine', 0, TRIAGE.text)


def test_install_skills_copies_into_each_harness_dir_once_and_replaces_only_piloops_own(
    harness_home: Settings, tmp_path: Path
) -> None:
    agents = harness_home.agent_skills_dir
    claude = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'skills'
    install.install_skills()
    assert printed().strip() == f'skills: piloop-operate, piloop-review, piloop-setup → {agents} (Agent Skills)', (
        'every harness that follows agentskills.io reads them, on PATH or not'
    )
    on_path('claude', 'codex', 'pi')
    (agents / 'piloop-old').mkdir(parents=True)
    (agents / 'theirs').mkdir()
    (agents / 'piloop-setup').rename(tmp_path / 'setup')
    (agents / 'piloop-setup').symlink_to(tmp_path / 'checkout')
    (claude / 'piloop-operate').mkdir(parents=True)
    (claude / 'piloop-operate' / 'SKILL.md').write_text('an older operate')
    assert skills.drift(claude) == ['piloop-operate out of date', 'piloop-review missing', 'piloop-setup missing']
    assert skills.drift(agents) == ['piloop-setup missing', 'piloop-old no longer shipped']
    install.install_skills()
    assert printed().splitlines() == [
        f'skills: piloop-operate, piloop-review, piloop-setup → {agents} (codex, pi)',
        f'skills: piloop-operate, piloop-review, piloop-setup → {claude} (claude)',
    ]
    assert skills.drift(claude) == skills.drift(agents) == []
    assert sorted(p.name for p in agents.iterdir()) == ['piloop-operate', 'piloop-review', 'piloop-setup', 'theirs']
    assert not (agents / 'piloop-setup').is_symlink()


def test_each_skills_directory_is_a_setting_so_a_second_install_keeps_its_skills_apart(
    harness_home: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's directory follows CLAUDE_CONFIG_DIR, unless PILOOP_CLAUDE_SKILLS_DIR names another one."""
    on_path('claude', 'codex')
    assert skills.targets(Settings())[tmp_path / 'claude' / 'skills'] == ['claude']
    monkeypatch.setenv('PILOOP_CLAUDE_SKILLS_DIR', str(tmp_path / 'canary'))
    monkeypatch.setenv('PILOOP_AGENT_SKILLS_DIR', str(tmp_path / 'canary'))
    assert skills.targets(Settings()) == {tmp_path / 'canary': ['claude', 'codex']}, 'one directory, read by both'
