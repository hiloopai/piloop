"""What the ``optimization`` campaign type runs in the daemon: its evaluation queue, and the items of its alarms."""

from datetime import datetime

from sqlalchemy import select

from piloop.daemon.loop import Consumer, Kinds, Phase, Turn
from piloop.db.models import NOT_RETIRED, OPTIMIZATION, Campaign, EventModel
from piloop.db.triggers.changelog import EvaluationCancelled
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded
from piloop.plugins.campaign_type.kit.evaluation.submissions import tell_scores
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.contribution import Contribution, DaemonHost
from piloop.plugins.executor.consumers import LIMITS

for kind in ('slots_idle.seen', 'slots_idle.raised', 'slots_idle.ended'):
    EventModel.retire(kind)  # 0.5 escalated free slots behind proposed ideas; an idea is never proposed now
for kind in ('low_yield.raised', 'low_yield.cleared'):
    EventModel.retire(kind)  # 0.6 escalated a low yield; stop rules and escalations cover a stuck campaign now

EVALUATIONS: Kinds = frozenset({EvaluationCancelled, MeasurementRecorded, *LIMITS})
"""What wakes the evaluation queue: a cancel to stop, a slot that an evaluation freed, or more slots. The executor's
``capacity_events`` say that it has more slots too."""


def contribution(host: DaemonHost) -> Contribution:
    """The consumers ``evaluations`` and ``scores``.

    ``evaluations`` stops the evaluations that someone cancelled, and submits those that a slot freed for. ``scores``
    tells each running worker where its submissions stand, on a score (``tell_scores``).
    """

    def evaluations(_turn: Turn) -> datetime | None:
        host.controller.stop_cancelled()
        host.controller.dispatch()
        return None

    def scores(_turn: Turn) -> datetime | None:
        with host.db.session() as s:
            for campaign in s.scalars(select(Campaign).where(NOT_RETIRED, Campaign.type == OPTIMIZATION)).all():
                if told := tell_scores(s, campaign, host.agents.steer_line_chars):
                    host.tracer.event('campaign.scores_told', campaign=campaign.name, messages=len(told))
            s.commit()
        return None

    wakes = EVALUATIONS | host.controller.executor.capacity_events()
    return Contribution(
        consumers=(
            Consumer('evaluations', wakes, evaluations, Phase.UPKEEP),
            Consumer('scores', frozenset({CandidateScored}), scores, Phase.IN_FLIGHT),
        )
    )
