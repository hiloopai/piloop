"""The ids and states of the ``candidates`` component: a candidate's id, origin, and why a verdict isn't the rule's."""

from enum import StrEnum
from typing import Final, NewType

CandidateId = NewType('CandidateId', int)


class Cause(StrEnum):
    """Why an ``interesting`` verdict is not the verdict rule's own.

    ``note``: an agent marked a regressed result as a lead (``piloop candidate note``, the ``Piloop-Note`` trailer).
    ``moved``: it kept against its own bar, but the head moved on, so it was merged onto the head.
    """

    NOTE = 'note'
    MOVED = 'moved'


class Origin(StrEnum):
    """Where a candidate comes from. Only a candidate from the ``baseline`` or a ``run`` is judged.

    ``baseline``: the repo's HEAD when the campaign was registered. ``run``: an implementer's commit. ``reference``: a
    starting point imported with ``piloop candidate import``, measured but never the head or a bar. ``recovered``: the
    daemon's commit of what an implementer left uncommitted when it stopped, or where a daemon stop cut its run, never
    declared ready, so never measured; a new run of the idea starts from it. ``skipped``: a commit marked
    ``Piloop-Eval: skip``, never measured. A ``recovered`` or ``skipped`` tree that a later commit reaches without the
    mark is measured and judged like a new one, and becomes ``run``.
    """

    BASELINE = 'baseline'
    RUN = 'run'
    REFERENCE = 'reference'
    RECOVERED = 'recovered'
    SKIPPED = 'skipped'


JUDGED: Final = frozenset({Origin.BASELINE, Origin.RUN})
"""The origins of the candidates that get a verdict. ``verdict IS NULL`` on one of them means that it is pending."""


class Outcome(StrEnum):
    """A verdict: what a rule gives, and what ``candidates.verdict`` holds. The column is NULL until a candidate is judged."""

    KEEP = 'keep'
    INTERESTING = 'interesting'
    DISCARD = 'discard'
    CRASH = 'crash'
