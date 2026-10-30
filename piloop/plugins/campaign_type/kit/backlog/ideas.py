"""Ideas: their links, where they come from and what each source produced.

Links (``idea_links``) are *after* links. The rule is on ``Idea`` in the models. This module holds its SQL and its
text: which open ideas a worker can claim (``claimable``), and the cell that shows an idea's links.

``ops`` holds the changes to an idea.
``yields`` measures each source by its keeps, not by the number of ideas it inserted. The overview and the research
pack show it.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import ColumnElement, case, distinct, exists, func, select
from sqlalchemy.orm import Session, aliased

from piloop.config import CONFIG_FILE, ConfigError
from piloop.db.models import Campaign
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.backlog.ids import SETTLED, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.backlog.wire import IdeaRow, IdeaView
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.tables import Run


def held() -> ColumnElement[bool]:
    """Select the ideas that an ``after`` link holds: one of their prerequisites is not settled (``SETTLED``)."""
    link, target = aliased(IdeaLink), aliased(Idea)
    return exists().where(
        link.idea_id == Idea.id,
        link.kind == LinkKind.AFTER,
        target.id == link.target_id,
        target.status.not_in(SETTLED),
    )


def claimable() -> ColumnElement[bool]:
    """Select the ideas that are ``open`` and not held."""
    return (Idea.status == IdeaStatus.OPEN) & ~held()


def check_tracks(s: Session, campaign: Campaign) -> None:
    """Refuse the campaign's new config when it drops a track that ideas of the campaign are on.

    An idea's runs are judged on its track, so an idea on a track that is gone fails a run that finishes, and a
    remeasure of its candidates cannot judge them. A track is named after its primary unless the primary says
    ``track``, so a new primary drops the old one's track.

    Raises:
        ConfigError: one line for each dropped track, with the ideas on it and how to keep it.
    """
    kept = {t.name for t in campaign.config.all_tracks}
    stranded: dict[TrackName, list[IdeaId]] = {}
    for idea_id, track in s.execute(select(Idea.id, Idea.track).where(Idea.campaign_id == campaign.id)):
        if track not in kept:
            stranded.setdefault(track, []).append(idea_id)
    if not stranded:
        return
    bad = [
        f'  track {track!r} is gone, and idea(s) {", ".join(f"#{i}" for i in ids)} are on it: keep its name with'
        f' `track = "{track}"` on the primary that replaces it, or move them first with `piloop idea track`'
        for track, ids in stranded.items()
    ]
    raise ConfigError(
        '\n'.join([f'{Path(campaign.path) / CONFIG_FILE} drops the track of ideas ({len(bad)} problem(s)):', *bad])
    )


def links(idea: Idea) -> str:
    """Format the idea's links as one short cell, or ``—`` if there are none.

    The parts are ``held by idea#3 (claimed), idea#5 (open)`` and ``after idea#1`` (prerequisites that are done or
    dropped), joined with ``; ``.
    """
    parts: list[str] = []
    holding = [t for t in idea.after if t.status not in SETTLED]
    done = [t for t in idea.after if t.status in SETTLED]
    if holding:
        parts.append('held by ' + ', '.join(f'idea#{t.id} ({t.status})' for t in holding))
    if done:
        parts.append('after ' + _refs(t.id for t in done))
    return '; '.join(parts) or '—'


def _refs(ids: Iterable[int]) -> str:
    return ', '.join(f'idea#{i}' for i in ids)


def source(created_by: str) -> str:
    """Return where an idea came from: its signature without the agent id (``research#12`` → ``research``)."""
    return created_by.partition('#')[0]


def added_by(s: Session, campaign_id: CampaignId) -> set[str]:
    """Return the signatures that added an idea to the campaign."""
    return set(s.scalars(select(Idea.created_by).where(Idea.campaign_id == campaign_id)))


@dataclass(frozen=True)
class Yield:
    """What one source's ideas produced: how many it filed, how many ran, and their candidates."""

    source: str
    ideas: int
    ran: int
    keeps: int
    interesting: int

    def line(self) -> str:
        return f'{self.source} {self.ideas} ideas, {self.ran} ran → {self.keeps} keep, {self.interesting} interesting'


def yields(s: Session, campaign_id: CampaignId) -> list[Yield]:
    """Count, for each source, the ideas, the ideas with a run, and their ``keep`` and ``interesting`` candidates.

    The result is sorted by keeps, then interesting candidates, then ideas, most first. A verdict counts as it is
    now, including overrides.
    """
    verdict = Candidate.verdict
    rows = s.execute(
        select(
            Idea.created_by,
            func.count(distinct(Run.id)),
            func.count(distinct(case((verdict == Outcome.KEEP, Candidate.id)))),
            func.count(distinct(case((verdict == Outcome.INTERESTING, Candidate.id)))),
        )
        .outerjoin(Run, Run.idea_id == Idea.id)
        .outerjoin(Candidate, Candidate.run_id == Run.id)
        .where(Idea.campaign_id == campaign_id)
        .group_by(Idea.id)
    )
    tally: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for created_by, runs, keeps, interesting in rows:
        counts = tally[source(created_by)]
        counts[0] += 1
        counts[1] += runs > 0
        counts[2] += keeps
        counts[3] += interesting
    found = [Yield(name, *counts) for name, counts in tally.items()]
    return sorted(found, key=lambda y: (-y.keeps, -y.interesting, -y.ideas, y.source))


def idea_row(idea: Idea) -> IdeaRow:
    """``idea`` as a list of ideas answers it."""
    return IdeaRow(
        id=idea.id,
        campaign=idea.campaign_id,
        title=idea.title,
        status=idea.status,
        priority=idea.priority,
        track=idea.track,
        created_by=idea.created_by,
        created=idea.created_at,
        after=[t.id for t in idea.after],
    )


def idea_view(idea: Idea) -> IdeaView:
    """``idea`` as the API answers it."""
    return IdeaView(
        id=idea.id,
        campaign=idea.campaign_id,
        title=idea.title,
        status=idea.status,
        priority=idea.priority,
        track=idea.track,
        created_by=idea.created_by,
    )


def open_ideas(s: Session, campaign: Campaign) -> tuple[Sequence[Idea], Sequence[Idea]]:
    """Return every ``open`` idea by priority, and the ones that no ``after`` link holds."""
    mine = select(Idea).where(Idea.campaign_id == campaign.id).order_by(Idea.priority, Idea.id)
    opened = s.scalars(mine.where(Idea.status == IdeaStatus.OPEN)).all()
    free = s.scalars(mine.where(claimable())).all()
    return opened, free
