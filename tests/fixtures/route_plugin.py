"""An escalation policy that serves an authenticated route, with ``piloop.capabilities`` alone, as a package would."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from piloop.capabilities import (
    REFUSALS_DOCUMENTED,
    Acting,
    Addressed,
    RouteHost,
    Routes,
    Structured,
    actor_access,
    reader,
)


class Greeting(BaseModel):
    campaign: str
    to: str


router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)


@router.post('/campaigns/{campaign}/greeting', operation_id='greeting', dependencies=[Depends(actor_access)])
def greeting(me: Acting, campaign: Addressed) -> Greeting:
    return Greeting(campaign=campaign.name, to=me.signature)


class Greeter(Structured):
    """``structured``, and a route that greets the caller by the actor it signs as."""

    def routes(self, host: RouteHost) -> Routes:
        return Routes(router)
