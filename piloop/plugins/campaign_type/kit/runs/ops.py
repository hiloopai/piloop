"""The operations of a worker on its own run: which run it is, and how it ends it."""

from typing import Literal

from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.ops import Refused, required_reason
from piloop.db.events import emit, latest
from piloop.db.models import AgentSession, AgentSessionState, Event, EventModel
from piloop.db.session import write_transaction
from piloop.ids import TargetRef
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.campaign_type.kit.evaluation.submissions import submitted
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS, RUN, RunEnding, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of


class RunEndRequested(EventModel):
    """On a run: its worker ended it (``piloop run end``). The daemon ends the worker after its ``end_grace``."""

    kind: Literal['run.end_requested'] = 'run.end_requested'
    how: RunEnding
    reason: str | None = None
    """Why the worker gave up. None for a submit."""


def run_of_actor(s: Session, actor: Actor) -> Run:
    """The open run that ``actor``, a worker, works on: queued or running.

    Raises:
        Refused: the actor is no agent of a run, or its run ended.
    """
    agent = actor.launched
    session = s.get(AgentSession, agent.session) if agent is not None else None
    run_id = run_of(session) if session is not None else None
    if run_id is None:
        raise Refused(f'{actor} has no run: only a worker has one')
    run = s.get_one(Run, run_id)
    if run.status not in OPEN_RUNS:
        raise Refused(f'run#{run.id} is {run.status} already')
    return run


def end_run(s: Session, how: RunEnding, reason: str | None, *, actor: Actor) -> Run:
    """End the actor's running run as ``how``: emit ``RunEndRequested`` on it. The caller commits.

    The daemon then ends the worker (``wind_down``), and the run closes as its end says (``run_outcome``). The check
    and the write are one write transaction, so a request either commits before the daemon ends the worker for a stop
    or a cancel, or is refused: it would undo that end.

    Raises:
        Refused: the actor has no open run, the daemon is ending the actor, its run is not running or was ended
            already, ``gave_up`` has no reason, or ``submitted`` comes from a run that submitted nothing
            (``[evaluator] scoring = "submit"``).
    """
    write_transaction(s)
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    run = run_of_actor(s, actor)
    if run.status is not RunStatus.RUNNING:
        raise Refused(f'run#{run.id} is {run.status}: a run ends while it runs')
    agent = actor.launched
    assert agent is not None  # run_of_actor found its session
    if (state := s.get_one(AgentSession, agent.session).state) is not AgentSessionState.RUNNING:
        raise Refused(f'{actor} is {state}: the daemon ended it before its run ended')
    if (asked := end_requested(s, run)) is not None:
        raise Refused(f'run#{run.id} was ended already: {asked.how}')
    match how, run.campaign.config.evaluator.scoring:
        case RunEnding.SUBMITTED, Scoring.SUBMIT if not submitted(s, run.id):
            raise Refused(
                f'run#{run.id} submitted nothing: `piloop submit` first, or `piloop run end gave_up --reason "…"`'
                ' to put its idea back on the board'
            )
        case _:
            pass
    why = required_reason(reason or '') if how is RunEnding.GAVE_UP else None
    emit(s, RunEndRequested(how=how, reason=why), TargetRef(RUN, run.id), actor)
    return run


def end_requested(s: Session, run: Run) -> RunEndRequested | None:
    """How the run's worker ended the run, or None while it did not."""
    found = latest(s, RunEndRequested, Event.target == TargetRef(RUN, run.id))
    return None if found is None else found[1]
