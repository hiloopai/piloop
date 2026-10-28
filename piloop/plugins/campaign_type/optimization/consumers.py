"""What the ``optimization`` campaign type runs in the daemon: its evaluation queue, and its idle slots' items."""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select

from piloop.daemon.loop import Consumer, Kinds, Phase, Soonest, Turn
from piloop.db.models import NOT_RETIRED, OPTIMIZATION, Campaign
from piloop.db.triggers.changelog import EvaluationCancelled
from piloop.plugins.campaign_type.consumers import campaign_kinds
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded
from piloop.plugins.campaign_type.optimization.idle import watch
from piloop.plugins.contribution import Contribution, DaemonHost
from piloop.plugins.executor.consumers import LIMITS

EVALUATIONS: Kinds = frozenset({EvaluationCancelled, MeasurementRecorded, *LIMITS})
"""What wakes the evaluation queue: a cancel to stop, a slot that an evaluation freed, or more slots. The executor's
``capacity_events`` say that it has more slots too."""


def contribution(host: DaemonHost) -> Contribution:
    """The consumers ``evaluations`` and ``idle_slots``.

    ``evaluations`` stops the evaluations that someone cancelled, and submits those that a slot freed for.
    ``idle_slots`` raises and clears the items of free slots behind proposed ideas (``idle.watch``). The campaigns'
    state wakes it, as it wakes the pass over the campaigns.
    """

    def evaluations(_turn: Turn) -> datetime | None:
        host.controller.stop_cancelled()
        host.controller.dispatch()
        return None

    def idle_slots(turn: Turn) -> datetime | None:
        later = Soonest()
        with host.db.session() as s:
            for campaign in s.scalars(select(Campaign).where(NOT_RETIRED, Campaign.type == OPTIMIZATION)).all():
                try:
                    cfg = campaign.config
                except ValidationError:  # the pass over the campaigns traces it: campaign.invalid_config
                    continue
                later.add(watch(s, campaign, cfg, turn.now))
            s.commit()
        return later.at

    wakes = EVALUATIONS | host.controller.executor.capacity_events()
    return Contribution(
        consumers=(
            Consumer('evaluations', wakes, evaluations, Phase.UPKEEP),
            Consumer('idle_slots', campaign_kinds(), idle_slots, Phase.PLAN),
        )
    )
