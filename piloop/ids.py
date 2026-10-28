"""The ids of rows, the names of roles and tracks, what a row is about, and the states that the API shows and takes.

Each id is a ``NewType`` of ``int``, so an idea's id does not type-check where a candidate's id goes. A name is a
``NewType`` of ``str``. A ``TargetRef`` is a kind and an id, written ``idea#12``.
"""

import re
from enum import StrEnum
from typing import Annotated, Final, NewType

from pydantic import AfterValidator, BeforeValidator, PlainSerializer, PositiveInt, StringConstraints
from pydantic.dataclasses import dataclass

type EnvName = Annotated[str, StringConstraints(pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')]
"""A POSIX environment variable name: a secret's, or one that an agent or a server reads. Every harness's ``${NAME}``
expansion accepts it."""
CampaignId = NewType('CampaignId', int)
AgentSessionId = NewType('AgentSessionId', int)
CommentId = NewType('CommentId', int)
JournalId = NewType('JournalId', int)
Revision = NewType('Revision', str)
"""The vcs provider's name for a version of a campaign's files: a commit SHA under ``git``."""

ROLE_NAME = r'^[a-z][a-z0-9_-]{0,19}$'
"""A role's name, which is also its config table, CLI name, signature and pane prefix."""
RoleName = NewType('RoleName', str)
"""A role's name: a role of a package, piloop's included, or a ``[plugins.role.<name>]`` table. It matches ``ROLE_NAME``."""

CAMPAIGN_NAME = re.compile(r'[A-Za-z0-9._-]+')
"""The characters of a campaign's name, which git refs carry."""


def checked_campaign_name(name: str) -> str:
    """Return ``name`` if it is a campaign's name. Raises ``ValueError`` saying why not."""
    if CAMPAIGN_NAME.fullmatch(name) is None:
        raise ValueError(f'{name!r} is no campaign name: use letters, digits, ".", "_" and "-"')
    if name.isdigit():
        raise ValueError(f'{name!r} is no campaign name: a reference of digits names a campaign by its id')
    return name


type CampaignName = Annotated[str, AfterValidator(checked_campaign_name)]
"""A campaign's name: ``CAMPAIGN_NAME``, and not all digits."""


class TargetKind(StrEnum):
    """What a comment or an event points at."""

    CAMPAIGN = 'campaign'
    IDEA = 'idea'
    RUN = 'run'
    CANDIDATE = 'candidate'
    MEASUREMENT = 'measurement'
    AGENT_SESSION = 'agent_session'
    COMMENT = 'comment'
    INSTANCE = 'instance'
    """A worker that a provisioner started (``worker_instances``)."""
    HOME = 'home'
    """The whole home, for an event about no row: a limit, a worker. Its one id is 1 (``HOME``). No comment is on it."""


@dataclass(frozen=True)
class TargetRef:
    """What a comment or an event is about: ``target_kind`` and ``target_id``, written ``idea#12``.

    A dataclass, so that it maps the two columns as one composite attribute.
    """

    kind: TargetKind
    id: PositiveInt

    def __str__(self) -> str:
        return f'{self.kind}#{self.id}'


HOME: Final = TargetRef(TargetKind.HOME, 1)
"""The target of an event about the whole home."""

REF_PATTERN = re.compile(r'(?P<kind>[a-z_]+)#(?P<id>[0-9]+)')


def _parsed(value: object) -> object:
    if not isinstance(value, str):
        return value
    if (found := REF_PATTERN.fullmatch(value)) is None:
        raise ValueError(f'{value!r} is not a reference like idea#12')
    return {'kind': found['kind'], 'id': int(found['id'])}


type Ref = Annotated[TargetRef, BeforeValidator(_parsed), PlainSerializer(str, return_type=str)]
"""A ``TargetRef`` that a model holds as its text, ``idea#12``."""


class CommentKind(StrEnum):
    """Why a comment was written. An ``escalation`` asks for a decision and is always in the human's inbox."""

    NOTE = 'note'
    FINDING = 'finding'
    DECISION = 'decision'
    ESCALATION = 'escalation'
    MESSAGE = 'message'
    """Someone's word to the orchestrator on a campaign (``piloop message``). The orchestrator answers it with
    ``piloop inbox reply``."""


class CampaignStatus(StrEnum):
    """Whether the daemon still requests work for a campaign. ``stopped`` after a ``[stop]`` condition held."""

    ACTIVE = 'active'
    STOPPED = 'stopped'
    RETIRED = 'retired'
    """Stopped for good (``piloop campaign retire``): it changes no more, lists leave it out unless asked, and another
    campaign may take its name. Its rows and events stay."""


class CampaignFiles(StrEnum):
    """Where a campaign's files are: its checkout, if piloop cloned it, and its directory under the home."""

    KEPT = 'kept'
    """On the disk: every campaign that is not retired."""
    PACKING = 'packing'
    """Retired, and on their way to the CAS."""
    FREED = 'freed'
    """In the CAS, and off the disk (``campaign.files_freed``)."""


LISTED: Final = (CampaignStatus.ACTIVE, CampaignStatus.STOPPED)
"""The statuses of the campaigns that a listing shows unless asked: all but ``retired``."""


class Ending(StrEnum):
    """How the daemon ends the agents of a stop or a cancel that someone requested."""

    WRAP_UP = 'wrap_up'
    """After the wrap-up prompt and its grace period: the agent may commit first."""
    NOW = 'now'
    """At once."""


class KeyOrigin(StrEnum):
    """Where the value of a live key of a provider comes from (``piloop limits``)."""

    DEFAULT = 'default'
    CONFIG = 'config.toml'
    LIMIT = 'limit'
