from pydantic import Field
from sqlalchemy import func, select

from piloop.api.views import Lines, Rendered
from piloop.plugins.campaign_type.kit.backlog.ideas import holders, links, open_ideas
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.text import NONE, age, cell


class Queue(Section):
    """The board: the ideas by status, what holds the held ones, and the claimable ideas by priority."""

    SHOWN_IN = frozenset({Surface.STATUS, Surface.SWEEP, Surface.RUN})
    ORDER = 150

    held: int = Field(default=5, ge=0)
    """How many held ideas the section names, each with what holds it. It counts the rest."""
    ideas: int = Field(default=8, ge=0)
    """How many claimable ideas it lists, most urgent first."""

    def render(self, r: Rendering) -> Rendered:
        s, campaign = r.s, r.campaign
        by_status = select(Idea.status, func.count()).where(Idea.campaign_id == campaign.id).group_by(Idea.status)
        counts: dict[IdeaStatus, int] = {status: n for status, n in s.execute(by_status)}
        opened, free = open_ideas(s, campaign)
        held = [i for i in opened if i not in free]
        waits = [
            f'idea#{i.id} on ' + ', '.join(f'idea#{t.id} ({t.status})' for t in holders(i)) for i in held[: self.held]
        ]
        more = f', {len(held) - self.held} more' if len(held) > self.held else ''
        shown = f' ({"; ".join(waits)}{more})' if waits else ''
        rest = ', '.join(f'{counts.get(status, 0)} {status}' for status in IdeaStatus if status is not IdeaStatus.OPEN)
        queue = [
            f'  - idea#{i.id} p{i.priority} "{cell(i.title, 60)}" by {i.created_by}, {age(i.created_at, r.now)} ago'
            + ('' if (linked := links(i)) == NONE else f'; {linked}')
            for i in free[: self.ideas]
        ]
        lines = (f'- Ideas: {len(free)} open, {len(held)} held{shown}; {rest}.', *queue)
        return Rendered(title='Queue', blocks=(Lines(lines=lines),))
