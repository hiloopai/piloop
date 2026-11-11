"""The pool's routes: a remeasure, and an exclusion."""

from fastapi import APIRouter, Depends

from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate
from piloop.plugins.campaign_type.kit.candidates.views import candidate_view
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer
from piloop.plugins.campaign_type.kit.evaluation import ops as evaluation_ops, task_holds
from piloop.plugins.campaign_type.kit.evaluation.measure import request_remeasure
from piloop.plugins.campaign_type.kit.pool import ops as pool_ops
from piloop.plugins.campaign_type.kit.pool.wire import ExcludeBody, RemeasureBody, Remeasured

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The pool's routes, beside the API's."""


@router.post('/candidates/{candidate}/remeasure', operation_id='remeasure', dependencies=[Depends(actor_access)])
def remeasure(s: Writing, me: Acting, candidate: AddressedCandidate, body: RemeasureBody) -> Remeasured:
    """Ask the daemon to evaluate a candidate more times, with the current evaluator. Its mean includes them."""
    task_holds.identify(s, candidate)
    task_holds.reset(s, candidate, resume=True)
    request_remeasure(s, candidate, me, body.repeats)
    s.commit()
    campaign = s.get_one(Campaign, candidate.campaign_id)
    held = evaluation_ops.measuring_held(s, campaign, campaign.config).strip() or None
    with db_feed.snapshot(s) as cursor:
        return Remeasured(cursor=cursor, candidate=candidate_view(candidate), held=held)


@router.post('/candidates/{candidate}/exclude', operation_id='exclude', dependencies=[Depends(actor_access)])
def exclude(s: Writing, me: Acting, candidate: AddressedCandidate, body: ExcludeBody) -> CandidateAnswer:
    """Take a candidate out of the race for the best, with the reason. Its scores stay in the pool."""
    pool_ops.exclude(s, candidate.id, body.reason, actor=me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return CandidateAnswer(cursor=cursor, candidate=candidate_view(candidate))
