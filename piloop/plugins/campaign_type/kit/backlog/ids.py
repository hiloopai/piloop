"""The ids and states of the ``backlog`` component: its ideas and their links."""

from enum import StrEnum
from typing import Final, NewType

from piloop.ids import TargetKind

IDEA: Final = TargetKind('idea')
"""An idea of the backlog (``ideas``). What its comments and events point at."""

IdeaId = NewType('IdeaId', int)


class IdeaStatus(StrEnum):
    """Where an idea is on the board.

    An ``open`` idea waits for a run, unless an ``after`` link holds it. A ``claimed`` idea has one run that works on
    it. When that run's commits are judged, the idea is ``done``, or ``open`` again for another attempt. A ``dropped``
    idea is off the board until someone opens it again.
    """

    OPEN = 'open'
    CLAIMED = 'claimed'
    DONE = 'done'
    DROPPED = 'dropped'


SETTLED: Final = (IdeaStatus.DONE, IdeaStatus.DROPPED)
"""The statuses of a prerequisite that free the ideas after it: it ran to its result, or someone took it off the board."""


class LinkKind(StrEnum):
    """How one idea relates to another in ``idea_links``: it runs *after* the target."""

    AFTER = 'after'
