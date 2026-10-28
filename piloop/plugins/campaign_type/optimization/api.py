"""Optimization's routes: its own, a note on a candidate, and those of the components that it uses."""

from fastapi import APIRouter, Depends

from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.plugins.campaign_type.kit.backlog import api as backlog_api
from piloop.plugins.campaign_type.kit.backlog.ideas import idea_view
from piloop.plugins.campaign_type.kit.candidates import api as candidates_api
from piloop.plugins.campaign_type.kit.candidates.api import AddressedCandidate
from piloop.plugins.campaign_type.kit.evaluation import api as evaluation_api
from piloop.plugins.campaign_type.kit.runs import api as runs_api
from piloop.plugins.campaign_type.kit.verdicts import api as verdicts_api
from piloop.plugins.campaign_type.kit.verdicts.verdict import candidate_view
from piloop.plugins.campaign_type.optimization import ops as optimization_ops
from piloop.plugins.campaign_type.optimization.wire import NoteBody, Noted

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""Optimization's routes, beside the API's."""


@router.post('/candidates/{candidate}/note', operation_id='note', dependencies=[Depends(actor_access)])
def note(s: Writing, me: Acting, candidate: AddressedCandidate, body: NoteBody) -> Noted:
    """Keep a regressed result in view as a lead, with follow-up seeds pinned to it."""
    _, seeds = optimization_ops.note_candidate(
        s, candidate.id, body.reason, actor=me, follow_ups=body.follow_ups, track=body.track
    )
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return Noted(cursor=cursor, candidate=candidate_view(candidate), seeds=[idea_view(i) for i in seeds])


ROUTERS: tuple[APIRouter, ...] = (
    backlog_api.router,
    candidates_api.router,
    verdicts_api.router,
    router,
    evaluation_api.router,
    runs_api.router,
)
"""Every route of optimization, its own and its components', in the order of the API's document."""


def _joined(routers: tuple[APIRouter, ...]) -> APIRouter:
    """One router with the routes of ``routers``.

    Each route has its prefix, dependencies and responses already. A flat list lets the server refuse a route whose
    method and path are taken, which it cannot see inside an included router.
    """
    joined = APIRouter()
    for each in routers:
        joined.routes.extend(each.routes)
    return joined


ROUTES = _joined(ROUTERS)
"""What optimization contributes to the server (``Contribution.router``)."""
