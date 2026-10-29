"""The mechanism of escalations: file one with its form, and answer one with a choice. The policy is a provider's.

``[plugins.escalation]`` of the campaign's ``piloop.toml`` chooses the provider (``piloop.plugins.escalation``). It
checks the form, writes its plain text, says when it reaches a human, and how it shows. Core stores the form on the
comment (``comments.form``) and never reads it.
"""

from dataclasses import dataclass

from pydantic import JsonValue, ValidationError
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from piloop.actor import Actor, System
from piloop.campaign.inbox import ack, campaign_of, root_of
from piloop.db.models import Campaign, Comment
from piloop.ids import CampaignId, CommentId, CommentKind, TargetKind, TargetRef
from piloop.plugins.escalation.contract import ESCALATIONS, Escalated, EscalationPolicy, EscalationView


class Refused(Exception):
    """An escalation that piloop does not file, or a choice that it does not take: the message says what to fix."""


DECISION_PRIORITY = 2
"""The inbox priority of the decision that a choice writes, as ``piloop inbox reply`` gives one by default."""


def campaign_of_target(s: Session, target: TargetRef) -> CampaignId | None:
    """The campaign that ``target`` belongs to, as ``inbox.campaign_of`` finds it for a comment on it."""
    found = s.scalar(select(campaign_of(literal(target.kind), literal(target.id))))
    return CampaignId(found) if found is not None else None


def policy(s: Session, campaign: CampaignId | None) -> EscalationPolicy:
    """The escalation provider of ``campaign``: its ``[plugins.escalation]``, else the default.

    A stored config that no longer validates (a provider of a package that is gone) falls back to the default, so an
    escalation still shows.
    """
    row = s.get(Campaign, campaign) if campaign is not None else None
    try:
        plugins = row.config.plugins if row is not None else {}
    except ValidationError:  # the campaigns' pass records this error for the campaign
        plugins = {}
    return ESCALATIONS.selected(plugins)


def escalated(s: Session, comment: Comment) -> Escalated:
    """``comment``, an escalation, as its provider reads it."""
    found = s.scalar(select(campaign_of(Comment.target_kind, Comment.target_id)).where(Comment.id == comment.id))
    return Escalated(
        comment=comment.id,
        campaign=CampaignId(found) if found is not None else None,
        author=comment.author,
        filed=comment.created_at,
        body=comment.body,
        form=comment.form,
    )


type Asked = JsonValue | EscalationView
"""What an escalation asks: an author's form as JSON, which the provider checks, or piloop's own words, which the
provider drafts into its form (``EscalationPolicy.draft``)."""


def escalate(s: Session, target: TargetRef, asked: Asked, *, author: Actor, priority: int) -> Comment:
    """File an escalation on ``target``, through its campaign's provider. The caller commits.

    Raises:
        Refused: An author's form is on a target of no campaign, or the provider refuses it: the message says what to
            fix.
    """
    form, body = _drafted(s, target, asked)
    comment = Comment(
        target=target, author=author.signature, kind=CommentKind.ESCALATION, priority=priority, body=body, form=form
    )
    s.add(comment)
    s.flush()
    return comment


def escalate_once(s: Session, target: TargetRef, asked: EscalationView, *, priority: int) -> Comment | None:
    """File piloop's own escalation on ``target``, unless one on it says the same already: None then. The caller commits."""
    form, body = _drafted(s, target, asked)
    said = select(Comment.id).where(
        Comment.target == target, Comment.kind == CommentKind.ESCALATION, Comment.body == body
    )
    if s.scalar(said.limit(1)) is not None:
        return None
    return escalate(s, target, asked, author=System.DAEMON, priority=priority)


def revise(s: Session, comment: Comment, asked: EscalationView) -> None:
    """Change what piloop's own escalation ``comment`` says, such as a count that grew. The caller commits."""
    comment.form, comment.body = _drafted(s, comment.target, asked)


def _drafted(s: Session, target: TargetRef, asked: Asked) -> tuple[JsonValue, str]:
    """The form that ``asked`` becomes with the provider of ``target``'s campaign, and its plain text."""
    campaign = campaign_of_target(s, target)
    chosen = policy(s, campaign)
    match asked:
        case EscalationView():
            form = chosen.draft(asked)
        case _ if campaign is None:
            raise Refused(f'{target} is in no campaign: escalate on your idea, run or campaign')
        case _:
            try:
                form = chosen.form(asked)
            except ValueError as e:
                raise Refused(f'the form is refused: {e}. `piloop escalate {target} --guide` shows the form') from e
    return form, chosen.body(form)


@dataclass(frozen=True)
class Settled:
    """An escalation that someone answered, or acknowledged, already."""

    by: str
    """The signature of whoever wrote its latest decision, else of whoever acknowledged it."""
    decision: str | None
    """The text of its latest decision, or None when it was only acknowledged."""


def settled(s: Session, item: Comment) -> Settled | None:
    """Whether ``item`` has a ``decision`` reply, or an acknowledgement: then a choice would be a second answer."""
    latest = select(Comment).where(Comment.in_reply_to == item.id, Comment.kind == CommentKind.DECISION)
    if (decision := s.scalars(latest.order_by(Comment.id.desc()).limit(1)).first()) is not None:
        return Settled(decision.author, decision.body)
    if item.acked_by is not None:
        return Settled(item.acked_by, None)
    return None


def choose(s: Session, comment_id: CommentId, choice: str, *, author: Actor) -> Comment:
    """Answer the escalation ``comment_id`` with ``choice``: a ``decision`` reply in its thread, then acknowledge it.

    Both happen in one transaction, signed as ``author``, and the decision is an inbox item of its own, as
    ``piloop inbox reply`` writes one.

    Raises:
        Refused: The comment is no escalation, or it offers no such choice.
    """
    item = s.get_one(Comment, comment_id)
    if item.kind != CommentKind.ESCALATION:
        raise Refused(f'comment#{comment_id} is a {item.kind}, not an escalation: answer it with `piloop inbox reply`')
    found = escalated(s, item)
    try:
        text = policy(s, found.campaign).choose(found, choice)
    except ValueError as e:
        raise Refused(str(e)) from e
    answer = Comment(
        target=TargetRef(TargetKind.COMMENT, root_of(s, item).id),
        in_reply_to=item.id,
        author=author.signature,
        kind=CommentKind.DECISION,
        priority=DECISION_PRIORITY,
        body=text,
    )
    s.add(answer)
    s.flush()
    ack(s, [item.id], author)
    return answer
