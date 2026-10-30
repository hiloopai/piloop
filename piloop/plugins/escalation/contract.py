"""The escalation capability: what an escalation holds, who sees it when, and how every surface shows it.

Core keeps the mechanism: an escalation is a comment of kind ``escalation``, its typed payload is ``comments.form``,
``piloop escalate`` files one, and ``piloop inbox choose`` answers one with a choice. Everything else is a provider's
policy: the fields of the form and their limits (``form``), when it reaches a human (``reach``), and the view that
the notifiers, ``piloop inbox show`` and the API render (``view``). ``[plugins.escalation]`` in ``piloop.toml``
chooses the provider of a campaign: ``structured`` by default.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, JsonValue

from piloop.ids import CampaignId, CommentId
from piloop.plugins.host import Capability, One, Parameters, Scope


class Choice(BaseModel):
    """One answer that an escalation offers."""

    model_config = ConfigDict(frozen=True)

    id: str
    """What picks it: ``piloop inbox choose <comment> <id>``, or a button."""
    label: str
    """What the human chooses, in one short sentence."""
    result: str
    """What happens then."""


class EscalationView(BaseModel):
    """An escalation as every surface shows it: Slack, a webhook, ``piloop inbox show``, a console."""

    model_config = ConfigDict(frozen=True)

    headline: str
    """The decision that is needed, in one sentence."""
    why: str
    """Why it is needed, in a few short sentences. Empty when the escalation says nothing more."""
    choices: tuple[Choice, ...] = ()
    """The answers it offers. Empty: the human answers in words, with a reply."""
    recommended: str | None = None
    """The id of the choice that its author recommends, or None."""
    details: str = ''
    """The evidence: ids, hashes, paths, logs. A surface shows it second, or on demand."""


@dataclass(frozen=True)
class Escalated:
    """An escalation that piloop has, as a provider reads it."""

    comment: CommentId
    campaign: CampaignId | None
    author: str
    """Who filed it: its signature, such as ``implementer#12``."""
    filed: datetime
    body: str
    form: JsonValue | None
    """What ``EscalationPolicy.form`` returned when it was filed. None for one that an older piloop filed as free text,
    which every surface shows as its body: piloop asks the provider of no view or choice for it."""


@dataclass(frozen=True)
class Now:
    """Send it to a human at once."""


@dataclass(frozen=True)
class At:
    """Send it to a human at ``at``, if nobody has acknowledged it by then."""

    at: datetime


@dataclass(frozen=True)
class Never:
    """It never goes to a human through the notifiers."""


type ToHuman = Now | At | Never


class EscalationPolicy(Parameters, ABC):
    """An escalation provider: the keys of ``[plugins.escalation]`` besides ``provider``, and its policy."""

    @abstractmethod
    def form(self, raw: JsonValue) -> JsonValue:
        """Check what an agent files, and return the form to store.

        Raises:
            ValueError: It is not a form of this provider: the message says what to fix, field by field.
        """

    @abstractmethod
    def draft(self, asked: EscalationView) -> JsonValue:
        """The form of an escalation that piloop itself files, from what it asks: no rule of wording applies to it."""

    @abstractmethod
    def guide(self) -> str:
        """What ``piloop escalate --guide`` prints: the fields, their limits, and an example."""

    @abstractmethod
    def body(self, form: JsonValue) -> str:
        """The plain text of ``form``, which ``comments.body`` keeps for a reader of SQL."""

    @abstractmethod
    def reach(self, item: Escalated, now: datetime) -> ToHuman:
        """When ``item`` goes to a human through the notifiers, while nobody has acknowledged it."""

    @abstractmethod
    def view(self, item: Escalated) -> EscalationView:
        """How every surface shows ``item``, which has its form."""

    @abstractmethod
    def choose(self, item: Escalated, choice: str) -> str:
        """The text of the ``decision`` that answers ``item`` with the choice ``choice``.

        Raises:
            ValueError: ``item`` offers no such choice: the message lists those it offers.
        """


ESCALATIONS = Capability(
    'escalation',
    EscalationPolicy,
    table=One(Scope.CAMPAIGN, default='structured'),
    group='piloop.escalations',
    noun='escalation policy',
)
"""Every escalation provider by name. ``structured``, built in and the default, asks for a decision in plain words."""
