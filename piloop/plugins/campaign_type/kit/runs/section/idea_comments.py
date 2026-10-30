from pydantic import Field
from sqlalchemy import func, select

from piloop.api.views import Lines, Rendered
from piloop.db.models import AgentSession, Comment
from piloop.ids import TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.runs.ids import RUN
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.section.contract import Rendering, Section, Surface


class IdeaComments(Section):
    """``[plugins.section.idea_comments]``: the comments on the reader's idea and on its runs, the latest last.

    A comment on a run is about the idea too, such as a human's decision on an earlier attempt, or a watchdog's finding
    on why it failed. A run's brief needs them: the next attempt is a new run.
    """

    SHOWN_IN = frozenset({Surface.RUN})
    ORDER = 5

    latest: int = Field(default=10, ge=1)
    """How many of the latest comments the section shows. It counts the older ones."""
    chars: int = Field(default=600, ge=1)
    """How many characters of each comment's body it shows. A longer one ends with how to read the rest."""

    def render(self, r: Rendering) -> Rendered | None:
        if r.reader is None or (agent := r.s.get(AgentSession, r.reader)) is None or (run := run_of(agent)) is None:
            return None
        if (idea_id := r.s.get_one(Run, run).idea_id) is None:
            return None
        on_idea = Comment.target == TargetRef(IDEA, idea_id)
        on_its_runs = (Comment.target_kind == RUN) & Comment.target_id.in_(select(Run.id).where(Run.idea_id == idea_id))
        about = on_idea | on_its_runs
        total = r.s.scalar(select(func.count()).select_from(Comment).where(about)) or 0
        if not total:
            return None
        shown = r.s.scalars(select(Comment).where(about).order_by(Comment.id.desc()).limit(self.latest)).all()[::-1]
        older = (
            [f'- {total - len(shown)} older comments: `SELECT body FROM comments` reads them.']
            if total > len(shown)
            else []
        )
        return Rendered(
            title='Comments on this idea and its runs',
            blocks=(Lines(lines=(*older, *(self._line(c) for c in shown))),),
        )

    def _line(self, c: Comment) -> str:
        where = f' on run#{c.target_id}' if c.target_kind == RUN else ''
        body = c.body if len(c.body) <= self.chars else f'{c.body[: self.chars]}… (comment#{c.id} has the rest)'
        return f'- {c.author} ({c.kind}{where}): {body}'
