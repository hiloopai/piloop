from operator import attrgetter

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.api.views import Rendered, Text
from piloop.db.models import AgentSession, Campaign
from piloop.ids import AgentSessionId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.campaign_type.optimization.history import history, latest_notes
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.wake import cost


class History(Section):
    """What was tried, for the reader of a run, in a budget: the ideas, the newest bests and the latest notes.

    The ideas whose runs made a new best come first, then the newest. The notes are those on the campaign, its ideas and its runs, newest
    first. The section shows as many of each as fit, one more of each at a time. ``piloop history`` lists every recent
    idea.
    """

    SHOWN_IN = frozenset({Surface.RUN})
    ORDER = 920

    budget: int = Field(default=1000, ge=1)
    """The size of the section in tokens, at about 4 characters each. The ideas, bests and notes that do not fit are
    left out, and the titles count the ideas and the bests."""
    findings: int = Field(default=10, ge=0)
    """How many of the latest notes on the campaign, its ideas and its runs the section lists at most."""

    def render(self, r: Rendering) -> Rendered:
        mine = _idea_of(r.s, r.reader)
        ranked = _ranked(r.s, r.campaign, mine)
        chosen = f'; besides idea#{mine.id}, those that made a new best, then the newest' if mine is not None else ''
        notes = latest_notes(r.s, r.campaign, self.findings)
        kept = len(new_bests(r.s, r.campaign))

        def text(n: int) -> str:
            shown = sorted(ranked[:n], key=attrgetter('id'))
            cfg = r.campaign.config
            return history(r.s, r.campaign, cfg, ideas=shown, notes=notes[:n], bests=min(n, kept), chosen=chosen)

        low, high = 0, max(len(ranked), len(notes), kept)  # the most of each that fit: text(low) fits, or low is 0
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
    """The campaign's ideas but ``mine``, most relevant first: those that made a new best, then the rest, newest first."""
    ideas = s.scalars(select(Idea).where(Idea.campaign_id == campaign.id).order_by(Idea.id.desc())).all()
    bests = [s.get_one(Candidate, e.target_id) for e, _ in new_bests(s, campaign)]
    kept = {c.run.idea_id for c in bests if c.run is not None}
    return sorted((i for i in ideas if mine is None or i.id != mine.id), key=lambda idea: idea.id not in kept)
