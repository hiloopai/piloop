"""The ids and states of the ``runs`` component: a run's status and end, and the role whose runs fill slots last."""

from enum import StrEnum
from typing import Final, NewType

from piloop.ids import (
    CommentKind,
    RoleName,
    TargetKind,
)

RUN: Final = TargetKind('run')
"""A run (``runs``). What its comments and events point at."""
FINDING: Final = CommentKind('finding')
"""An implementer's report on its idea: it settles the run as finished (``FindingWritten``)."""

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


class RunEnd(StrEnum):
    """How a run ended. ``runs.error`` adds the detail, when there is one.

    ``finished``: its implementer left its finding, or committed and exited cleanly. ``failed``: its process failed, it
    made no commit, or only the daemon's commit of what it left uncommitted (the error says which). ``timed_out``: its
    role's timeout. ``awaiting_answer``: its implementer's escalation waits for an answer (the error names it).
    ``cancelled``: someone cancelled it. ``campaign_stopped``, ``daemon_stopped``, ``stale`` (a daemon died while it
    ran) and ``worker_lost``: something else ended it. ``role_removed``: ``piloop init`` removed its role while it was
    queued. ``not_started``: no agent of it started to work. Only the first four are attempts at the idea.
    """

    FINISHED = 'finished'
    FAILED = 'failed'
    TIMED_OUT = 'timed_out'
    AWAITING_ANSWER = 'awaiting_answer'
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


ATTEMPTS: Final = frozenset({RunEnd.FINISHED, RunEnd.FAILED, RunEnd.TIMED_OUT, RunEnd.AWAITING_ANSWER})
"""The ends of a run that tried its idea. The others say that the run was stopped, or that no agent started to work."""


END_STATUS: Final = {RunEnd.FINISHED: RunStatus.SUCCEEDED, RunEnd.TIMED_OUT: RunStatus.TIMED_OUT}
"""The status of a run by its end, where it is not ``failed``."""
