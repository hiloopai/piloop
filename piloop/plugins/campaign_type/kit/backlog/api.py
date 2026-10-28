"""The backlog's routes: ideas and seeds, their links, bases, tracks and statuses."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from piloop.api.contract import (
    Problem,
    Reasoned,
)
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Addressed, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.net import Refusal
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ideas import Definition, idea_view
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.backlog.wire import (
    BaseBody,
    Defined,
    DevelopBody,
    IdeaAnswer,
    IdeaBody,
    SeedBody,
    StatusBody,
    StatusChanged,
    TrackBody,
    TrackChanged,
)

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The backlog's routes, beside the API's."""


def idea_addressed(s: Writing, idea: IdeaId) -> Idea:
    """The idea that the path names. Refuses one that the home does not have."""
    return _idea(s, idea)


def target_addressed(s: Writing, target: IdeaId) -> Idea:
    """The idea that the path links to. Refuses one that the home does not have."""
    return _idea(s, target)


def _idea(s: Session, idea: IdeaId) -> Idea:
    if (found := s.get(Idea, idea)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_IDEA, f'no idea#{idea}')
    return found


AddressedIdea = Annotated[Idea, Depends(idea_addressed)]


Target = Annotated[Idea, Depends(target_addressed)]


@router.post('/campaigns/{campaign}/ideas', operation_id='add_idea', dependencies=[Depends(actor_access)])
def add_idea(s: Writing, me: Acting, campaign: Addressed, body: IdeaBody) -> Defined:
    """Add a defined idea, which the daemon schedules in priority order once it is ready."""
    idea, review = backlog_ops.add_idea(
        s,
        campaign.id,
        body.title,
        actor=me,
        hypothesis=body.hypothesis,
        plan=body.plan,
        provenance=body.provenance,
        family=body.family,
        priority=body.priority,
        harness=body.harness,
        base=body.base,
        proposed=body.proposed,
        track=body.track,
        after=body.after,
        supersedes=body.supersedes,
    )
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return Defined(cursor=cursor, idea=idea_view(idea), review=review)


@router.post('/campaigns/{campaign}/seeds', operation_id='file_seed', dependencies=[Depends(actor_access)])
def file_seed(s: Writing, me: Acting, campaign: Addressed, body: SeedBody) -> IdeaAnswer:
    """File a seed, for research to develop into a defined idea or to reject."""
    idea = backlog_ops.file_seed(
        s, campaign.id, body.hunch, actor=me, provenance=body.provenance, base=body.base, track=body.track
    )
    return _idea_answer(s, idea)


@router.post('/ideas/{idea}/develop', operation_id='develop', dependencies=[Depends(actor_access)])
def develop(s: Writing, me: Acting, idea: AddressedIdea, body: DevelopBody) -> Defined:
    """Develop a seed into a defined idea that the daemon can schedule."""
    definition = Definition(body.hypothesis, body.evidence, body.check, body.metric, body.effect, body.plan)
    developed, review = backlog_ops.develop_seed(
        s,
        idea.id,
        definition,
        actor=me,
        title=body.title,
        family=body.family,
        base=body.base,
        priority=body.priority,
        proposed=body.proposed,
        after=body.after,
        supersedes=body.supersedes,
    )
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return Defined(cursor=cursor, idea=idea_view(developed), review=review)


@router.post('/ideas/{idea}/status', operation_id='set_status', dependencies=[Depends(actor_access)])
def set_status(s: Writing, me: Acting, idea: AddressedIdea, body: StatusBody) -> StatusChanged:
    """Set an idea's status, with a note on the idea that says why."""
    was = backlog_ops.set_idea_status(s, idea.id, body.status, reason=body.reason, actor=me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return StatusChanged(cursor=cursor, idea=idea_view(idea), was=was)


@router.put('/ideas/{idea}/links/{kind}/{target}', operation_id='link', dependencies=[Depends(actor_access)])
def link(s: Writing, me: Acting, idea: AddressedIdea, kind: LinkKind, target: Target, body: Reasoned) -> IdeaAnswer:
    """Link an idea to another of its campaign: it runs after the target, or supersedes it."""
    backlog_ops.link(s, idea.id, kind, target.id, reason=body.reason, actor=me)
    return _idea_answer(s, idea)


@router.delete('/ideas/{idea}/links/{kind}/{target}', operation_id='unlink', dependencies=[Depends(actor_access)])
def unlink(s: Writing, me: Acting, idea: AddressedIdea, kind: LinkKind, target: IdeaId, body: Reasoned) -> IdeaAnswer:
    """Delete an idea's link."""
    backlog_ops.unlink(s, idea.id, kind, target, reason=body.reason, actor=me)
    return _idea_answer(s, idea)


@router.post('/ideas/{idea}/base', operation_id='set_base', dependencies=[Depends(actor_access)])
def set_base(s: Writing, me: Acting, idea: AddressedIdea, body: BaseBody) -> IdeaAnswer:
    """Set the candidate that an idea's runs start from, or clear the pin."""
    backlog_ops.set_base(s, idea.id, body.base, reason=body.reason, actor=me)
    return _idea_answer(s, idea)


@router.post('/ideas/{idea}/track', operation_id='set_track', dependencies=[Depends(actor_access)])
def set_track(s: Writing, me: Acting, idea: AddressedIdea, body: TrackBody) -> TrackChanged:
    """Move an idea to another track of its campaign, with a note on the idea that says why."""
    was = backlog_ops.set_track(s, idea.id, body.track, reason=body.reason, actor=me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return TrackChanged(cursor=cursor, idea=idea_view(idea), was=was)


def _idea_answer(s: Session, idea: Idea) -> IdeaAnswer:
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return IdeaAnswer(cursor=cursor, idea=idea_view(idea))
