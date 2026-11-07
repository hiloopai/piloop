from operator import attrgetter

from sqlalchemy import select

from piloop.api.views import Lines, Rendered
from piloop.campaign import inbox
from piloop.campaign.inbox import campaign_of
from piloop.db.models import Comment
from piloop.plugins.escalation.contract import DECISION, ESCALATION
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.settings import settings
from piloop.text import age, cell

COMMENTS_SHOWN = 8


class Inbox(Section):
    """The inbox items that wait for the reader, and the latest decisions and escalations, newest first.

    A sweep reads it so that it does not repeat an escalation, and a human sees what waits for an acknowledgement.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 200

    def render(self, r: Rendering) -> Rendered:
        s, campaign, now = r.s, r.campaign, r.now
        # what waits for the agent who reads, else for the people (``piloop status``)
        waiting = inbox.waiting_for(s, r.brief.agent.party if r.brief else inbox.HUMAN, campaign_id=campaign.id)
        unacked = {c.id for c, _ in waiting}
        said = Comment.id.in_(unacked) | Comment.kind.in_((DECISION, ESCALATION))
        rows = s.scalars(
            select(Comment)
            .where(said, campaign_of(Comment.target_kind, Comment.target_id) == campaign.id)
            .order_by(Comment.id.desc())
            .limit(COMMENTS_SHOWN)
        ).all()
        asked = [c for c, _ in waiting if c.kind == ESCALATION]
        # an escalation that waits drops off the newest-first list below when more comments follow it, so count it here
        oldest = min(asked, key=attrgetter('id'), default=None)
        escalations = (
            f'; {len(asked)} escalation{"s" if len(asked) > 1 else ""}, the oldest comment#{oldest.id}'
            f' {age(oldest.created_at, now)} old'
            if oldest is not None
            else ''
        )
        lines = [
            f'- comment#{c.id} {c.kind}'
            f'{" (p" + str(c.priority) + ", unacknowledged)" if c.id in unacked else ""}'
            f' on {c.target} by {c.author}, {age(c.created_at, now)} ago: {cell(c.body, settings.agents.detail_chars)}'
            for c in rows
        ]
        title = f'Inbox ({len(waiting)} unacknowledged{escalations}), decisions and escalations'
        return Rendered(title=title, blocks=(Lines(lines=tuple(lines or ['- None.'])),))
