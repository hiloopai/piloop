"""The briefs of the roles: the prompt, the context and what the agent may write (https://piloop.autoresearch.inc/concepts/roles).

Every role shares one lifecycle (``piloop.agents.launcher``). A role differs in its ``[plugins.role.<name>]`` table
(``piloop.plugins.role``), its triggers (``piloop.plugins.trigger``) and the content of its brief, which this module composes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import NamedTuple

from covalence import Cid
from sqlalchemy.orm import Session

from piloop.actor import Actor, Agent
from piloop.config import BRIEF_FILE, CampaignConfig
from piloop.db.models import AgentSession, Campaign
from piloop.ids import RoleName
from piloop.plugins.cas.contract import Cas
from piloop.plugins.harness.contract import Harness
from piloop.plugins.role.contract import Files, RoleConfig, SweepRole
from piloop.plugins.section.contract import Briefing, Guide, Rendering, Section
from piloop.plugins.vcs.contract import VCS, prompted

PILOOP_HINT = '"$PILOOP_BIN"'
"""How a brief runs piloop: the worker sets ``PILOOP_BIN`` for every agent (``piloop.plugins.executor.kit.jobs.own_cli``)."""


@dataclass(frozen=True)
class Role:
    """The part of a brief that changes with the role. ``Launcher.launch`` does everything else.

    It comes from the role's ``[plugins.role.<kind>]`` table (``role``).
    """

    kind: RoleName
    """The role's name in ``piloop.toml``, and so ``agent_sessions.kind``, the signature of its agents
    (``<kind>#<agent session id>``) and the source of the ideas they file."""
    prompt: Traversable
    """The file that starts the brief: in the repo, or a resource of the role's package."""
    incident: Traversable | None
    """The file that follows ``prompt`` in an incident sweep, one that got anomalies. None: nothing follows."""
    routine: Traversable | None
    """The file that follows ``prompt`` in every other sweep. None for a role that does runs, or a role without one."""
    ideas: str
    """How this role gives work to the implementers, as the context block of the brief says it.

    ``{piloop}`` is the CLI and ``{campaign}`` is the campaign's name.
    """


IDEAS: dict[Files, str] = {
    Files.NOTHING: (
        '- A hunch worth a run of its own goes in a note, with pointers (ids, paths, the number). You file no ideas.'
    ),
    Files.IDEAS: (
        "- Add an idea: `{piloop} idea add {campaign} '<title>' --hypothesis '…' --plan '…' --from '<the evidence,"
        " with ids>'` (`--help` for links, a base, a priority)."
    ),
}
"""The ideas line of the brief for each value of ``files``."""


def role(cfg: CampaignConfig, kind: RoleName, repo: Path) -> Role:
    """Return the role of ``[plugins.role.<kind>]`` in a campaign at ``repo``.

    Raises:
        KeyError: ``piloop.toml`` has no such role.
    """
    table = cfg.role(kind)
    prompt = _prompt(repo, table.prompt, table.PROMPT)
    assert prompt is not None  # RoleConfig refuses a role with neither
    incident = routine = None
    if isinstance(table, SweepRole):
        incident = _prompt(repo, table.incident_prompt, table.INCIDENT_PROMPT)
        routine = _prompt(repo, table.routine_prompt, table.ROUTINE_PROMPT)
    ideas = table.IDEAS or IDEAS[filed(table)]
    return Role(kind, prompt, incident, routine, ideas)


def filed(table: RoleConfig) -> Files:
    """What the agents of a role may file with ``piloop idea``: a sweep's ``files``.

    A role that does runs files nothing for others: its agent proposes only its own run's idea (``ImplementerRole.IDEAS``).
    """
    match table:
        case SweepRole(files=files):
            return files
        case _:
            return Files.NOTHING


def _prompt(repo: Path, configured: str | None, own: Traversable | None) -> Traversable | None:
    """Return the file of a prompt key: the configured file in the repo, else the role's own, else None."""
    return repo / configured if configured is not None else own


def role_of(s: Session, agent_session: AgentSession) -> Role:
    """Return the role of a row: the role of its kind in its campaign."""
    campaign = s.get_one(Campaign, agent_session.campaign_id)
    return role(campaign.config, agent_session.kind, Path(campaign.path))


def harness_of(s: Session, agent_session: AgentSession) -> Harness | None:
    """Return the definition of the harness that a row runs, from its campaign's config.

    None before the launcher picks a harness, and when ``piloop.toml`` no longer names it (a row keeps only the name).
    """
    if agent_session.harness is None or agent_session.campaign_id is None:
        return None
    try:
        return s.get_one(Campaign, agent_session.campaign_id).config.harness_for(agent_session.harness)
    except KeyError:
        return None


def files_of(cfg: CampaignConfig, actor: Actor) -> Files | None:
    """Return what ``actor`` may file with ``piloop idea``, by the role in its signature (``watchdog#4``).

    None for an actor that is not a role, such as ``orchestrator`` or ``human``: it may file anything.
    """
    if (agent := actor.launched) is None:
        return None
    table = cfg.roles.all().get(agent.role)
    return None if table is None else filed(table)


def read_brief(cas: Cas, brief: Cid) -> str:
    """The text of the brief at ``brief`` in ``cas``.

    Raises:
        LookupError: the CAS lacks it.
    """
    if (data := cas.get(brief)) is None:
        raise LookupError(f'the CAS lacks the brief {brief}')
    return data.decode()


def briefing(role: Role, campaign: Campaign, agent: Agent, *, reason: str, lines: Sequence[str]) -> Briefing:
    """The agent that a brief is for, with ``lines`` of its campaign's type under the tracks and how it files ideas.

    With several tracks, the context lists them. A worker learns its own when it claims its idea.
    """
    cfg = campaign.config
    ideas = role.ideas.format(piloop=PILOOP_HINT, campaign=campaign.name)
    return Briefing(agent, reason, (*([tracks_line(cfg)] if cfg.multitrack else []), ideas, *lines))


class BriefSections(NamedTuple):
    """A brief's sections in markdown."""

    whole: str
    """Every section."""
    resumed: str
    """The sections that are not guides: the brief of an agent that resumes a session, which has the guides."""


def render_sections(sections: Sequence[Section], r: Rendering) -> BriefSections:
    """Render ``sections`` once, for a whole brief and for a resumed one."""
    found = [(section, out.markdown()) for section in sections if (out := section.render(r)) is not None]
    return BriefSections(
        '\n\n'.join(text for _, text in found),
        '\n\n'.join(text for section, text in found if not isinstance(section, Guide)),
    )


def compose_brief(role: Role, campaign: Campaign, sections: str, *, incident: bool = False) -> str:
    """Compose a brief: the role prompt, the campaign brief, the role's addendum, then ``sections``.

    A sweep also gets its role's incident or routine prompt after the role prompt: the incident prompt when it got
    anomalies (``incident``). A worker gets its track's addendum when it claims its idea (``track_brief``).
    """
    repo = Path(campaign.path)
    cfg = campaign.config
    addendum = cfg.role(role.kind).addendum
    follows = role.incident if incident else role.routine
    vcs = VCS.selected(cfg.plugins)
    parts = [
        prompted(role.prompt.read_text().rstrip(), vcs),
        prompted(follows.read_text().rstrip(), vcs) if follows is not None else '',
        (repo / BRIEF_FILE).read_text().rstrip(),
        (repo / addendum).read_text().rstrip() if addendum else '',
        sections,
    ]
    return '\n\n'.join(p for p in parts if p) + '\n'


def compose_resumed(table: RoleConfig, agent: Agent, sections: str) -> str:
    """Compose the brief of an agent that resumes its role's last session: the role's resume message, then ``sections``.

    The session holds the rest of the brief already, from the agent that started it.
    """
    return f'{table.resume_message.format(agent=agent.signature)}\n\n{sections}\n'


def tracks_line(cfg: CampaignConfig) -> str:
    """Return the campaign's tracks in one line for a brief. Every idea names one. A worker learns its own at its claim."""
    tracks = '; '.join(f'{t.name} (primary {t.primary.name}, {t.primary.direction} is better)' for t in cfg.all_tracks)
    return (
        f'- Tracks: {tracks}. Every idea names its track (`--track <name>`); every keep must also hold the'
        " other tracks' primaries within their margins and floors."
    )
