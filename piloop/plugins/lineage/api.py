"""The route of the lineage capability: ``GET /v1/campaigns/{campaign}/lineage``, which every provider shares."""

from http import HTTPStatus

from fastapi import APIRouter, Depends

from piloop.api.contract import Problem
from piloop.api.views import InvalidConfig
from piloop.daemon.api import REFUSALS_DOCUMENTED, Addressed, Writing, reader
from piloop.db import feed as db_feed
from piloop.net import Refusal
from piloop.plugins.lineage.contract import Lineage, instance_of
from piloop.reports.feed import configured

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The lineage's route, beside the API's."""


@router.get('/campaigns/{campaign}/lineage', operation_id='lineage')
def lineage(s: Writing, campaign: Addressed) -> Lineage:
    """The campaign as a graph of its nodes and their parents, from the instance of its ``[plugins.lineage] use``.

    Refuses a campaign whose stored config no longer validates.
    """
    if isinstance(invalid := configured(s, campaign), InvalidConfig):
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'{invalid.problem}; fix it with {invalid.fix}')
    name, provider = instance_of(campaign)
    with db_feed.snapshot(s) as cursor:
        return Lineage(cursor=cursor, provider=name, graph=provider.graph(s, campaign))
