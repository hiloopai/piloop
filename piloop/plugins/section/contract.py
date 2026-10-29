"""The sections: the titled blocks that a campaign's readers see (https://piloop.autoresearch.inc/concepts/roles#sections).

A section is a provider of ``SECTIONS``: its fields are its parameters, from ``[plugins.section.<name>]``. ``piloop
status``, the API and a UI show the sections of ``[plugins.section] use``. A brief shows its role's
``[plugins.role.<name>] sections``. A list that is not set shows the installed sections that declare its surface
(``Section.SHOWN_IN``), in their ``ORDER``. Each reader gets the sections in the order of the list, and leaves out a
section with nothing to show. A package adds a section with an entry point in the group of ``SECTIONS``: nothing in
piloop changes.

A section composes the blocks of ``piloop.api.views`` (``Text``, ``Lines``, ``Figures``, ``Table``). Each reader
renders each kind of block its own way, in one place: ``markdown`` for a brief and ``piloop status``, and the UI's own
renderer.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from piloop.actor import Agent
from piloop.api.views import Rendered, Text
from piloop.ids import AgentSessionId
from piloop.plugins.cas.contract import Cas
from piloop.plugins.host import Capability, Many, Parameters, Pick, Scope, Use
from piloop.plugins.vcs.contract import VCS, Vcs, prompted

if TYPE_CHECKING:  # piloop.db.models types campaigns.config with the sections: only the annotations name the rows
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign


@dataclass(frozen=True)
class Briefing:
    """The agent that a brief is for: what its ``context`` section says of it."""

    agent: Agent
    reason: str
    """Why the agent started."""
    lines: tuple[str, ...] = ()
    """What the campaign's type adds to the context: the lines of the agent's run, or of its sweep."""


@dataclass(frozen=True)
class Rendering:
    """What a section reads: core makes one for each rendering of a list of sections.

    A section reads with ``s`` and writes nothing.
    """

    s: Session
    """The session to read with. A section writes nothing."""
    now: datetime
    """The time of the rendering. A section that shows an age reads this, not the clock."""
    campaign: Campaign
    """The campaign, with its config (``campaign.config``)."""
    workspace: Path
    """The campaign's shared workspace, where agents leave notes and artifacts for each other."""
    cas: Cas
    """The home's CAS: the agents' sessions are there (``agent_sessions.session_file``)."""
    given: tuple[str, ...] = ()
    """The anomaly lines that the agent who reads got to investigate, in order. Empty for every other reader."""
    brief: Briefing | None = None
    """The agent who reads, when the reader is an agent's brief. None for every other reader."""

    @property
    def reader(self) -> AgentSessionId | None:
        """The session of the agent who reads: a section can show what is near its work. None when no agent reads."""
        return None if self.brief is None else self.brief.agent.session


class Surface(StrEnum):
    """Where sections are shown. A list that is not set shows the sections that name its surface."""

    STATUS = 'status'
    """``piloop status``, the API's overview and a UI: ``[plugins.section] use``."""
    SWEEP = 'sweep'
    """The brief of a sweeping role: ``[plugins.role.<name>] sections``."""
    RUN = 'run'
    """The brief of a role that does runs: ``[plugins.role.<name>] sections``."""


class Section(Parameters, ABC):
    """``[plugins.section.<name>]``: a section, which a list of sections names.

    A section declares where it is shown by default and in what order, so that installing a package adds it there,
    and a list in the configuration replaces those defaults.
    """

    SHOWN_IN: ClassVar[frozenset[Surface]] = frozenset()
    """The surfaces whose list shows the section, with its defaults, when the list is not set. Empty: only a list that
    names it shows it."""
    ORDER: ClassVar[int] = 500
    """Where the section goes in a default list: a lower number comes first. The built-in sections leave gaps,
    so that a package can put one between two of them."""

    @abstractmethod
    def render(self, r: Rendering) -> Rendered | None:
        """Return the section now, or None when it has nothing to show: then the reader leaves it out."""


CAMPAIGN_ID = '<campaign id>'
"""Where a guide's example names the campaign. A brief fills it in, ``piloop schema`` leaves it."""
PRIMARY = '<primary metric>'
"""Where a guide's example names the primary metric. A brief fills it in, ``piloop schema`` leaves it."""


class Guide(Section):
    """A section of reference: how to do one thing with piloop, the same in every brief of a campaign.

    A brief shows its role's guides before its context, in the order of the role's list. ``piloop schema`` prints
    every installed guide, the ones that no brief shows included. The text is a markdown resource of the guide's
    package, with ``{vcs:<part>}``, ``CAMPAIGN_ID`` and ``PRIMARY`` in it.
    """

    TITLE: ClassVar[str]
    """The guide's heading."""
    TEXT: ClassVar[Traversable]
    """The guide's text, without its heading."""

    def text(self, vcs: Vcs, campaign_id: str = CAMPAIGN_ID, primary: str = PRIMARY) -> str:
        """The guide's text, with what ``vcs`` writes, the campaign's id and its primary metric filled in."""
        text = prompted(self.TEXT.read_text().strip(), vcs)
        return text.replace(CAMPAIGN_ID, campaign_id).replace(PRIMARY, primary)

    def render(self, r: Rendering) -> Rendered:
        cfg = r.campaign.config
        text = self.text(VCS.selected(cfg.plugins), str(r.campaign.id), cfg.all_tracks[0].primary.name)
        return Rendered(title=self.TITLE, blocks=(Text(text=text),))


def guides() -> list[str]:
    """Every installed guide, by ``ORDER`` and then by name."""
    found = [(p.model.ORDER, name) for name, p in SECTIONS.providers().items() if issubclass(p.model, Guide)]
    return [name for _, name in sorted(found)]


def defaults(surface: Surface) -> list[str]:
    """The sections that ``surface`` shows when its list is not set: the guides first, each by ``ORDER``, then by name.

    They are every installed section with ``surface`` in its ``SHOWN_IN``.
    """
    shown = [
        (not issubclass(p.model, Guide), p.model.ORDER, name)
        for name, p in SECTIONS.providers().items()
        if surface in p.model.SHOWN_IN
    ]
    return [name for *_, name in sorted(shown)]


def of_status() -> list[str]:
    """The sections of ``piloop status`` when ``[plugins.section] use`` is not set."""
    return defaults(Surface.STATUS)


SECTIONS = Capability(
    'section', Section, table=Many(Scope.CAMPAIGN, use=Use(Scope.CAMPAIGN, Pick.ORDERED, defaults=of_status))
)
"""Every section by name: ``[plugins.section.<name>]``, and each provider that needs no keys. ``[plugins.section]
use`` lists those of ``piloop status``, and a role's ``sections`` those of its briefs."""

SectionName = Annotated[str, SECTIONS.ref]
"""The name of a section of the campaign, in a list of sections."""


def rendered(sections: Sequence[Section], r: Rendering) -> list[Rendered]:
    """Render ``sections`` in order, without the ones that have nothing to show."""
    return [found for section in sections if (found := section.render(r)) is not None]


def markdown(sections: Sequence[Section], r: Rendering) -> str:
    """Render ``sections`` as one markdown document for a brief: each section's ``markdown``, a blank line between."""
    return '\n\n'.join(found.markdown() for found in rendered(sections, r))
