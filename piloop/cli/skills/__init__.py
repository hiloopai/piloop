"""Install the orchestrator skills (``piloop-setup``, ``piloop-operate``, ``piloop-review``) for every harness.

A skill is an Agent Skill (agentskills.io): a ``SKILL.md`` that drives piloop through its CLI only, so every harness with
a shell reads the same text. The installed packages compose each skill from their parts (``SKILLS``): core's part of
``piloop-operate`` is the inbox loop, and the optimization campaign type adds ideas, runs and verdicts. ``install``
writes the skills into the Agent Skills directory, which any harness that follows agentskills.io reads, and into the
user skills directory of each harness on PATH (``Harness.skills_dir``). ``drift`` finds a copy that is missing or out
of date, for ``piloop doctor``. The installed CLI writes its own skills, so the skills always describe the commands of
that CLI.
"""

import shutil
from collections import defaultdict
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from pydantic import InstanceOf, TypeAdapter

from piloop.plugins.harness.contract import each_provider
from piloop.plugins.host import PluginError, Slot
from piloop.settings import Settings

PREFIX = 'piloop-'
"""Every skill that piloop installs is named ``piloop-*``. A directory with this prefix in a skills directory is
piloop's."""
SKILL_FILE = 'SKILL.md'


@dataclass(frozen=True)
class SkillPart:
    """A part of a skill that a package adds: an entry point in ``piloop.skills``, named after the part.

    A skill is the text of its parts in ``order``, then by name, a blank line between two. The part that comes first
    starts with the skill's frontmatter.
    """

    skill: str
    """The skill's name, ``piloop-*``: its directory in a skills directory."""
    order: int
    """Where the part goes in its skill: a lower number comes first. piloop's parts leave gaps, so that a package can
    put one between two of them."""
    text: Traversable
    """The part's markdown: a resource of its package."""
    replaces: tuple[str, ...] = ()
    """The names of the parts that this part takes the place of. A name that no package installs is ignored."""


def _not_piloops(name: str, part: SkillPart) -> str | None:
    return None if part.skill.startswith(PREFIX) else f'{part.skill!r} is no skill of piloop: name it {PREFIX}*'


SKILLS: Slot[SkillPart] = Slot('skills', TypeAdapter(InstanceOf[SkillPart]), 'skill part', _not_piloops)
"""Each part of a skill, by its name: entry points ``piloop.skills``."""

_HERE = files(__package__)
OPERATE = SkillPart('piloop-operate', 100, _HERE / 'operate.md')
"""The skill's start, the loop and the status."""
CONTROL = SkillPart('piloop-operate', 250, _HERE / 'control.md')
"""Pause, resume and stop a campaign, cancel an agent."""
ESCALATIONS = SkillPart('piloop-operate', 300, _HERE / 'escalations.md')
"""The inbox, and how to decide an escalation."""
DECISIONS = SkillPart('piloop-operate', 400, _HERE / 'decisions.md')
"""Messages, the record of each decision, the surfacing policy, and the agents."""
RULES = SkillPart('piloop-operate', 700, _HERE / 'rules.md')
"""What never to change by hand, and who changed what."""


def composed() -> dict[str, str]:
    """Return the text of each skill that the installed packages compose, by name.

    Raises:
        PluginError: A part is refused, or two parts replace one.
    """
    parts = SKILLS.loaded()
    replacers: dict[str, list[str]] = defaultdict(list)
    for name, part in parts.items():
        for replaced in part.replaces:
            replacers[replaced].append(name)
    if clashes := {replaced: names for replaced, names in replacers.items() if len(names) > 1}:
        replaced, names = next(iter(sorted(clashes.items())))
        raise PluginError(f'{" and ".join(sorted(names))} both replace {replaced!r}: uninstall one')
    texts: dict[str, list[str]] = defaultdict(list)
    for _, name in sorted((part.order, name) for name, part in parts.items() if name not in replacers):
        texts[parts[name].skill].append(parts[name].text.read_text().strip())
    return {skill: '\n\n'.join(texts[skill]) + '\n' for skill in sorted(texts)}


def targets(s: Settings) -> dict[Path, list[str]]:
    """Return the Agent Skills directory and that of each harness on PATH, each with the harnesses on PATH that read it."""
    found: dict[Path, list[str]] = {s.agent_skills_dir: []}
    for name, harness in each_provider().items():
        if shutil.which(harness.executable):
            found.setdefault(harness.skills_dir(s), []).append(name)
    return found


def readers(harnesses: list[str]) -> str:
    """Who reads a skills directory, for a line of output: its harnesses on PATH, else the Agent Skills standard."""
    return ', '.join(harnesses) or 'Agent Skills'


def drift(target: Path) -> list[str]:
    """Return how the piloop skills in ``target`` differ from the composed ones. Empty when they are current."""
    shipped = composed()
    problems = []
    for name, text in shipped.items():
        if not (target / name).is_dir():
            problems.append(f'{name} missing')
        elif _files(target / name) != {Path(SKILL_FILE): text.encode()}:
            problems.append(f'{name} out of date')
    return problems + [
        f'{p.name} no longer shipped' for p in sorted(target.glob(f'{PREFIX}*')) if p.name not in shipped
    ]


def install(target: Path) -> None:
    """Make the ``piloop-*`` skills in ``target`` the composed ones. Skills that piloop no longer ships are removed."""
    target.mkdir(parents=True, exist_ok=True)
    for existing in target.glob(f'{PREFIX}*'):
        _remove(existing)
    for name, text in composed().items():
        (target / name).mkdir()
        (target / name / SKILL_FILE).write_text(text)


def _files(root: Path) -> dict[Path, bytes]:
    return {f.relative_to(root): f.read_bytes() for f in root.rglob('*') if f.is_file()}


def _remove(path: Path) -> None:
    """Remove a copy of a skill, or a symlink to a checkout that someone made."""
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)
