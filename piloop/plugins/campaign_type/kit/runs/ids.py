"""The ids and states of the ``runs`` component: a run's status and end, and the role whose runs fill slots last."""

from enum import StrEnum
from typing import Final, NewType

from piloop.ids import (
    RoleName,
    TargetKind,
)

RUN: Final = TargetKind('run')
"""A run (``runs``). What its comments and events point at."""

RunId = NewType('RunId', int)


IMPLEMENTER: Final = RoleName('implementer')
"""The role that run slots fill last, and the role of a run whose row predates its role (``runs.role``)."""


class RunStatus(StrEnum):
    """The lifecycle of one attempt at an idea."""

    QUEUED = 'queued'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    TIMED_OUT = 'timed_out'


OPEN_RUNS = (RunStatus.QUEUED, RunStatus.RUNNING)


FAILED_RUNS = (RunStatus.FAILED, RunStatus.TIMED_OUT)


FINISHED_RUNS = (RunStatus.SUCCEEDED, *FAILED_RUNS)


class RunEnding(StrEnum):
    """How a worker ends its run (``piloop run end``): the only way a run ends before its timeout."""

    SUBMITTED = 'submitted'
    """Its work is done: what it saved is its result."""
    GAVE_UP = 'gave_up'
    """It stops without a result: its idea goes back to the board for another worker."""


class RunEnd(StrEnum):
    """How a run ended. ``runs.error`` adds the detail, when there is one.

    ``finished``: its worker ended it as submitted (``piloop run end submitted``). ``gave_up``: its worker ended it
    without a result (the error says why). ``failed``: its process failed, or ended without ``piloop run end`` (the
    error says which). ``timed_out``: its role's timeout. ``cancelled``: someone cancelled it. ``campaign_stopped``,
    ``daemon_stopped``, ``stale`` (a daemon died while it ran) and ``worker_lost``: something else ended it.
    ``role_removed``: ``piloop init`` removed its role while it was queued. ``not_started``: no agent of it started to
    work. Only the first four are attempts at an idea.
    """

    FINISHED = 'finished'
    GAVE_UP = 'gave_up'
    FAILED = 'failed'
    TIMED_OUT = 'timed_out'
    CANCELLED = 'cancelled'
    CAMPAIGN_STOPPED = 'campaign_stopped'
    DAEMON_STOPPED = 'daemon_stopped'
    STALE = 'stale'
    ROLE_REMOVED = 'role_removed'
    NOT_STARTED = 'not_started'
    WORKER_LOST = 'worker_lost'

    @property
    def status(self) -> RunStatus:
        """The ``runs.status`` of a run that ended so."""
        return END_STATUS.get(self, RunStatus.FAILED)


ATTEMPTS: Final = frozenset({RunEnd.FINISHED, RunEnd.GAVE_UP, RunEnd.FAILED, RunEnd.TIMED_OUT})
"""The ends of a run that tried its idea. The others say that the run was stopped, or that no agent started to work."""


END_STATUS: Final = {RunEnd.FINISHED: RunStatus.SUCCEEDED, RunEnd.TIMED_OUT: RunStatus.TIMED_OUT}
"""The status of a run by its end, where it is not ``failed``."""
