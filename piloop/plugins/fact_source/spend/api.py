"""The route of ``spend``: ``GET /v1/campaigns/{campaign}/spend``, what a campaign spent."""

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from piloop.config import ConfigError, explain
from piloop.daemon.api import REFUSALS_DOCUMENTED, Reading, Viewed, reader
from piloop.db.models import utcnow
from piloop.plugins.fact_source.spend import Spend, spend_of

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The spend's route, beside the API's."""


@router.get('/campaigns/{campaign}/spend', operation_id='spend')
def spend(s: Reading, campaign: Viewed) -> Spend | None:
    """What the campaign spent, in USD: each fact that its ``spend`` source names, their sum, and the sum per keep.

    null: the campaign's ``[plugins.fact_source] use`` has no ``spend`` source.
    """
    try:
        cfg = campaign.config
    except ValidationError as e:
        raise ConfigError(explain('the stored config', e)) from e
    return spend_of(s, campaign, cfg, utcnow())
