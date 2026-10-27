"""The ids and states of the ``backlog`` component: its ideas and their links."""

from enum import StrEnum
from typing import NewType

IdeaId = NewType('IdeaId', int)


class IdeaStatus(StrEnum):
    """The lifecycle of an idea. The scheduler picks up ``ready`` ideas in priority order.

    A ``seed`` is a rough idea with pointers. An agent whose role's ``files`` allows it files one (``piloop idea
    seed``). Research develops it into a defined idea (``piloop idea develop``, which makes it ``ready`` or
    ``proposed``), or rejects it.
    A ``proposed`` idea is defined and waits for a human to approve it.
    """

    SEED = 'seed'
    PROPOSED = 'proposed'
    READY = 'ready'
    SCHEDULED = 'scheduled'
    DONE = 'done'
    PARKED = 'parked'
    REJECTED = 'rejected'


class LinkKind(StrEnum):
    """How one idea relates to another in ``idea_links``: it runs *after* the target, or it *supersedes* the target."""

    AFTER = 'after'
    SUPERSEDES = 'supersedes'
