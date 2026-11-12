"""What the ``accumulating`` campaign type does in the daemon's campaigns' pass: ``optimization``'s, but for submissions.

A submission is not scored on its own. One merge job per campaign at a time merges what waits onto the trunk, measures
the merge once, and decides it (``trunk``).
"""

from collections.abc import Collection
from concurrent.futures import Future
from contextlib import closing
from functools import partial
from pathlib import Path

from sqlalchemy.orm import Session

from piloop.campaign.campaigns import vcs_of
from piloop.db.models import Campaign
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.accumulating.trunk import decide, head, start
from piloop.plugins.campaign_type.contract import CampaignType, TypeHost
from piloop.plugins.campaign_type.kit.evaluation import submissions
from piloop.plugins.campaign_type.kit.runs.controller import Measuring
from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner


class AccumulatingPlanner(OptimizationPlanner):
    """The ``accumulating`` type at work in one daemon process: ``optimization``'s hooks, with the trunk's merge job."""

    def __init__(self, host: TypeHost, kind: CampaignType) -> None:
        super().__init__(host, kind)
        self._merging: dict[CampaignId, Future[None]] = {}
        """The merge job in flight of each campaign."""

    def _score_submissions(self, s: Session, campaign: Campaign, held_tasks: Collection[str]) -> None:
        """Start the campaign's merge job when submissions wait and none is in flight (``merge``)."""
        busy = self._merging.get(campaign.id)
        if (busy is not None and not busy.done()) or not submissions.waiting(s, campaign):
            return
        self._tracer.event('trunk.merge', campaign=campaign.name)
        self._merging[campaign.id] = self._host.submit(partial(self.merge, campaign.id))

    def in_flight(self) -> dict[str, Future[None]]:
        """The scoring in flight, and each campaign's merge job."""
        self._merging = {c: f for c, f in self._merging.items() if not f.done()}
        return super().in_flight() | {f'trunk#{c}': f for c, f in self._merging.items()}

    def merge(self, campaign_id: CampaignId) -> None:
        """Measure the trunk, then merge what waits onto it, measure the merge, and decide it. Never raises.

        The trunk is measured first, under the current evaluator: the merge is taken on its primary against the trunk's.
        A measurement that does not end in a result (a stop, a cancel, a new evaluator) leaves the submissions waiting,
        and the next pass starts again.
        """
        db = self._controller.launcher.db
        with self._tracer.span('piloop.trunk.merge', campaign=campaign_id) as span:
            try:
                with db.session() as s:
                    campaign = s.get_one(Campaign, campaign_id)
                    trunk, vcs, path = head(s, campaign), vcs_of(campaign), Path(campaign.path)
                    trunk_id = None if trunk is None else trunk.id
                if trunk_id is None or (ended := self._controller.measure_once(trunk_id)) is not Measuring.MEASURED:
                    return
                with closing(vcs.open(path)) as repo, db.session() as s:
                    batch = start(s, repo, s.get_one(Campaign, campaign_id))
                    s.commit()
                if batch is None:
                    return
                span.set(merge=batch.merge, members=len(batch.members))
                if (ended := self._controller.measure_once(batch.merge)) is not Measuring.MEASURED:
                    span.set(status=ended)
                    return
                with db.session() as s:
                    span.set(advanced=decide(s, batch))
                    s.commit()
            except Exception as e:  # the thread's last frame: what failed goes into the span, never lost
                span.error(e)
