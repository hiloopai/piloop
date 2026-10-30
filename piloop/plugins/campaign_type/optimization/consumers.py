"""What the ``optimization`` campaign type runs in the daemon: its evaluation queue, and the items of its alarms."""

from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select

from piloop.daemon.loop import Consumer, Kinds, Phase, Turn
from piloop.db.models import NOT_RETIRED, OPTIMIZATION, Campaign, EventModel
from piloop.db.triggers.changelog import EvaluationCancelled
from piloop.plugins.campaign_type.consumers import campaign_kinds
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded
from piloop.plugins.campaign_type.optimization import yields
from piloop.plugins.campaign_type.optimization.trigger.low_yield import LowYield
from piloop.plugins.contribution import Contribution, DaemonHost
from piloop.plugins.executor.consumers import LIMITS

for kind in ('slots_idle.seen', 'slots_idle.raised', 'slots_idle.ended'):
    EventModel.retire(kind)  # 0.5 escalated free slots behind proposed ideas; an idea is never proposed now

EVALUATIONS: Kinds = frozenset({EvaluationCancelled, MeasurementRecorded, *LIMITS})
"""What wakes the evaluation queue: a cancel to stop, a slot that an evaluation freed, or more slots. The executor's
``capacity_events`` say that it has more slots too."""


def contribution(host: DaemonHost) -> Contribution:
    """The consumers ``evaluations`` and ``yield_alarm``.

    ``evaluations`` stops the evaluations that someone cancelled, and submits those that a slot freed for.
    ``yield_alarm`` raises and clears the escalation of a spell of low yield (``yields.watch``). The campaigns' state
    wakes it, as it wakes the pass over the campaigns.
    """

    def evaluations(_turn: Turn) -> datetime | None:
        host.controller.stop_cancelled()
        host.controller.dispatch()
        return None

    def yield_alarm(turn: Turn) -> datetime | None:
        with host.db.session() as s:
            for campaign in s.scalars(select(Campaign).where(NOT_RETIRED, Campaign.type == OPTIMIZATION)).all():
                try:
                    cfg = campaign.config
                except ValidationError:  # the pass over the campaigns traces it: campaign.invalid_config
                    continue
                yields.watch(s, campaign, LowYield.of(cfg), turn.now)
            s.commit()
        return None

    wakes = EVALUATIONS | host.controller.executor.capacity_events()
    return Contribution(
        consumers=(
            Consumer('evaluations', wakes, evaluations, Phase.UPKEEP),
            Consumer('yield_alarm', campaign_kinds(), yield_alarm, Phase.PLAN),
        )
    )
