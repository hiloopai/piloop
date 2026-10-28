"""The verdicts' routes: a remeasure next to the bar, and a verdict set by hand."""

from fastapi import APIRouter, Depends

from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops
from piloop.plugins.campaign_type.kit.evaluation.measure import request_remeasure
from piloop.plugins.campaign_type.kit.verdicts import ops as verdicts_ops
from piloop.plugins.campaign_type.kit.verdicts.judge import bar_of
from piloop.plugins.campaign_type.kit.verdicts.verdict import candidate_view, rejudgeable
from piloop.plugins.campaign_type.kit.verdicts.wire import Merge, RemeasureBody, Remeasured, VerdictBody, VerdictChanged

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The verdicts' routes, beside the API's."""


@router.post('/candidates/{candidate}/remeasure', operation_id='remeasure', dependencies=[Depends(actor_access)])
def remeasure(s: Writing, me: Acting, candidate: AddressedCandidate, body: RemeasureBody) -> Remeasured:
    """Ask the daemon to measure a candidate again, next to its bar, with the current evaluator."""
    request_remeasure(s, candidate, me, body.repeats)
    s.commit()
    campaign = s.get_one(Campaign, candidate.campaign_id)
    held = evaluation_ops.measuring_held(s, campaign, campaign.config).strip() or None
    bar = None if candidate.parent is None else bar_of(s, candidate.parent).id
    with db_feed.snapshot(s) as cursor:
        return Remeasured(
            cursor=cursor, candidate=candidate_view(candidate), bar=bar, held=held, rejudged=rejudgeable(candidate)
        )


@router.post('/candidates/{candidate}/verdict', operation_id='set_verdict', dependencies=[Depends(actor_access)])
def set_verdict(s: Writing, me: Acting, candidate: AddressedCandidate, body: VerdictBody) -> VerdictChanged:
    """Override a candidate's judgement, with the reason. The verdict is then the caller's."""
    done = verdicts_ops.set_verdict(s, candidate.id, body.verdict, reason=body.reason, actor=me)
    s.commit()
    rebased = done.rebased
    merged = None if rebased is None or rebased.rebased is None else Merge(candidate=rebased.rebased, head=rebased.head)
    with db_feed.snapshot(s) as cursor:
        return VerdictChanged(cursor=cursor, candidate=candidate_view(candidate), was=done.old, merge=merged)
