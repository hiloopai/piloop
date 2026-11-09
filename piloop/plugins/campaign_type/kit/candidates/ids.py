"""The ids and states of the ``candidates`` component: a candidate's id and its origin."""

from enum import StrEnum
from typing import Final, NewType

from piloop.ids import TargetKind

CANDIDATE: Final = TargetKind('candidate')
"""A candidate (``candidates``). What its comments and events point at."""

CandidateId = NewType('CandidateId', int)


class Origin(StrEnum):
    """Where a candidate comes from.

    ``baseline``: the repo's HEAD when the campaign was registered. ``run``: an implementer's commit. ``reference``: a
    starting point imported with ``piloop candidate import``, scored like any other. ``recovered``: the daemon's commit of what an implementer left uncommitted where a
    daemon stop cut its run, never measured; a new run of the idea starts from it.
    ``skipped``: a commit that its run did not submit, never measured. A ``recovered`` or ``skipped`` tree that a later
    submission reaches is measured like a new one, and becomes ``run``.
    """

    BASELINE = 'baseline'
    RUN = 'run'
    REFERENCE = 'reference'
    RECOVERED = 'recovered'
    SKIPPED = 'skipped'


SCORED: Final = frozenset({Origin.BASELINE, Origin.RUN})
"""The origins of the candidates that the daemon scores by itself: the baseline, and what a run submits."""
