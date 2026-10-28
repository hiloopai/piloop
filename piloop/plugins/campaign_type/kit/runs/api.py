"""The runs' routes: a run's cancel and extension."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from piloop.agents.results import extend
from piloop.api.contract import (
    CancelBody,
    Problem,
)
from piloop.campaign.stop import request_cancel
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.db.models import AgentSession, AgentSessionState
from piloop.net import Refusal
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS, RunId
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.runs.wire import ExtendBody, Extension, RunAnswer

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The runs' routes, beside the API's."""


def run_addressed(s: Writing, run: RunId) -> Run:
    """The run that the path names. Refuses one that the home does not have."""
    if (found := s.get(Run, run)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_RUN, f'no run#{run}')
    return found


AddressedRun = Annotated[Run, Depends(run_addressed)]


@router.post('/runs/{run}/cancel', operation_id='cancel_run', dependencies=[Depends(actor_access)])
def cancel_run(s: Writing, me: Acting, run: AddressedRun, body: CancelBody) -> RunAnswer:
    """Cancel a queued or running run: a queued one never starts, a running implementer ends as ``ending`` says."""
    if run.status not in OPEN_RUNS:
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'run#{run.id} is not queued or running')
    request_cancel(s, run, me, ending=body.ending)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return RunAnswer(cursor=cursor, run=run.id)


@router.post('/runs/{run}/extend', operation_id='extend_run', dependencies=[Depends(actor_access)])
def extend_run(s: Writing, me: Acting, run: AddressedRun, body: ExtendBody) -> Extension:
    """Give a running run's implementer more time: its deadline moves by ``by``, and a note tells it so."""
    running = (AgentSession.run_id == run.id) & (AgentSession.state == AgentSessionState.RUNNING)
    agent = s.scalars(select(AgentSession).where(running)).first()
    if agent is None:
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'run#{run.id} has no running implementer')
    total = extend(s, agent, body.by, me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return Extension(cursor=cursor, run=run.id, agent=agent.id, seconds=total)
