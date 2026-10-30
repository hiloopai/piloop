from operator import attrgetter

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.api.views import Rendered, Text
from piloop.db.models import AgentSession, Campaign
from piloop.ids import AgentSessionId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.campaign_type.kit.verdicts.judge import keeps
from piloop.plugins.campaign_type.optimization.history import history
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.wake import cost


class History(Section):
    """What was tried, for the reader of a run: the kept ideas, then the newest, in a budget.

    Then the kept lineage, the starting points and the latest findings. ``piloop history`` lists every recent idea.
    """

    SHOWN_IN = frozenset({Surface.RUN})
    ORDER = 920

    budget: int = Field(default=1000, ge=1)
    """The size of the section in tokens, at about 4 characters each. The ideas that do not fit are left out, and the
    table's title counts them."""
    findings: int = Field(default=10, ge=0)
    """How many of the latest findings on the campaign the section lists."""

    def render(self, r: Rendering) -> Rendered:
        mine = _idea_of(r.s, r.reader)
        ranked = _ranked(r.s, r.campaign, mine)
        chosen = f'; besides idea#{mine.id}, the keeps, then the newest' if mine is not None else ''

        def text(count: int) -> str:
            shown = sorted(ranked[:count], key=attrgetter('id'))
            return history(r.s, r.campaign, r.campaign.config, ideas=shown, findings=self.findings, chosen=chosen)

        low, high = 0, len(ranked)  # the most ideas that fit: text(low) fits, or low is 0
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if cost(text(middle).splitlines()) <= self.budget else (low, middle - 1)
        return Rendered(title='History', blocks=(Text(text=text(low).strip()),))


def _idea_of(s: Session, reader: AgentSessionId | None) -> Idea | None:
    """The idea of the reader's run, or None for a reader without one."""
    if reader is None or (agent := s.get(AgentSession, reader)) is None or run_of(agent) is None:
        return None
    return s.get_one(Run, run_of(agent)).idea


def _ranked(s: Session, campaign: Campaign, mine: Idea | None) -> list[Idea]:
    """The campaign's ideas but ``mine``, most relevant first: the kept ones, then the rest, newest first."""
    ideas = s.scalars(select(Idea).where(Idea.campaign_id == campaign.id).order_by(Idea.id.desc())).all()
    kept = {c.run.idea_id for c in s.scalars(keeps(campaign)) if c.run is not None}
    return sorted((i for i in ideas if mine is None or i.id != mine.id), key=lambda idea: idea.id not in kept)
