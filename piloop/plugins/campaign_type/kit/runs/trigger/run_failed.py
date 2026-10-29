from datetime import timedelta
from typing import ClassVar

from piloop.ids import TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.trigger.contract import Fire, Pass, Trigger
from piloop.plugins.trigger.kit.incidents import DETAIL_CHARS, Incident, investigate
from piloop.text import cell


class RunFailed(Trigger):
    """A run failed or timed out, and no agent of the campaign got it yet. The agent gets it to investigate.

    The row gets one ``anomaly.reported`` event for each such run. For the watchdog, that makes it an incident sweep.
    """

    INVESTIGATES: ClassVar[bool] = True

    def fire(self, p: Pass) -> Fire | None:
        failed = p.s.scalars(campaign_runs(p.campaign.id).where(Run.end.in_((RunEnd.FAILED, RunEnd.TIMED_OUT))))
        found = [
            Incident(
                TargetRef(RUN, run.id),
                run.finished_at or run.created_at,
                'failed',
                f'{run.end} on {run.harness} (idea#{run.idea_id}): {cell(run.error or "no error recorded", DETAIL_CHARS)}',
            )
            for run in failed
        ]
        return investigate(p, self, found)

    def due_in(self, p: Pass) -> timedelta | None:
        return None
