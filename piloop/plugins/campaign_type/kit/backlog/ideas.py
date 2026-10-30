"""Ideas: their links, the actions on them, where they come from and what each source produced.

Links (``idea_links``) are *after* and *supersedes*. The rules are on ``Idea`` in the models. This module holds
their SQL and their text. The scheduler uses it to find the ``ready`` ideas that can run and to reject superseded
ideas. The status and history renderers use it to show the links in one cell. Add the rule for a new kind of link
here.

``ops`` holds the changes to an idea.
``yields`` measures each source by its keeps, not by the number of ideas it inserted. The overview and the research
pack show it.

Seeds: every role except the implementer can file a hunch with pointers (``seed``). Research develops a seed into a
defined idea (``develop``) or rejects it. ``defined_by`` returns the agents that added a defined idea to the campaign.
This is how piloop measures the work of a research sweep.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import CTE, ColumnElement, case, distinct, exists, func, select
from sqlalchemy.orm import Session, aliased

from piloop.actor import System
from piloop.campaign import escalations
from piloop.config import CONFIG_FILE, ConfigError
from piloop.db.events import has
from piloop.db.models import Campaign, Comment, Event
from piloop.db.triggers import DAEMON_ACTOR
from piloop.db.triggers.changelog import IdeaStatusChanged
from piloop.ids import CampaignId, CommentKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.backlog.wire import IdeaRow, IdeaView
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.judge import NEVER_RAN
from piloop.plugins.escalation.contract import Choice, EscalationView


def unblocked() -> ColumnElement[bool]:
    """Select the ideas whose ``after`` links all point to ``done`` ideas.

    A prerequisite in any other status still blocks. One that ended without a keep never releases its dependents:
    ``park_stranded`` parks them.
    """
    return ~_blocked_by(IdeaStatus.DONE)


def runnable() -> ColumnElement[bool]:
    """Select the ideas that are ``ready`` and unblocked. The queue trigger takes only these."""
    return (Idea.status == IdeaStatus.READY) & unblocked()


RUN_ON: Final = (IdeaStatus.DONE, IdeaStatus.SCHEDULED, IdeaStatus.READY)
"""The statuses of the prerequisites that settle without anyone: done, or one that runs and then is done."""


def waiting() -> ColumnElement[bool]:
    """Select the ``ready`` ideas that are blocked only by prerequisites that settle on their own, through any chain.

    A prerequisite settles on its own when it is ``scheduled``, or ``ready`` and itself waits on no other kind. So the
    ideas become runnable as runs finish. ``low_water`` counts them as backlog, together with the runnable ideas,
    because no research sweep is needed before they run. Without this, a long run with many ideas ``after`` it starts
    a sweep on every pass.
    """
    return (Idea.status == IdeaStatus.READY) & ~unblocked() & Idea.id.not_in(select(_held().c.id))


def _held() -> CTE:
    """The ideas that do not settle on their own: each in no status of ``RUN_ON``, and each after one, at any depth.

    The links have no cycles (``ops.link`` refuses one).
    """
    link = aliased(IdeaLink)
    roots = select(Idea.id).where(Idea.status.not_in(RUN_ON)).cte('held', recursive=True)
    return roots.union(select(link.idea_id).where(link.kind == LinkKind.AFTER, link.target_id == roots.c.id))


def _blocked_by(*settled: IdeaStatus) -> ColumnElement[bool]:
    """Select the ideas that have an ``after`` link to an idea whose status is not in ``settled``."""
    link, target = aliased(IdeaLink), aliased(Idea)
    return exists().where(
        link.idea_id == Idea.id,
        link.kind == LinkKind.AFTER,
        target.id == link.target_id,
        target.status.not_in(settled),
    )


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


def park_stranded(s: Session, campaign_id: CampaignId) -> dict[IdeaId, IdeaId]:
    """Park each ``ready`` idea that never ran and runs ``after`` a prerequisite that ended without a keep.

    Such a prerequisite is ``parked`` (a cancel parks it), ``rejected``, or ``done`` after runs that kept nothing. Its
    dependents were meant to build on its change, which never landed, so they would block forever or start on a failed
    premise. A prerequisite set ``done`` that never ran still releases them: that is a human's decision. The ideas after
    a parked one are parked in the same call. Each prerequisite that ended without a keep gets one escalation, on it,
    that asks whether to run the ideas that it parked, its chain included, without it.

    Returns:
        ``{parked idea id: the prerequisite that stranded it}``.
    """
    link, target = aliased(IdeaLink), aliased(Idea)
    ran = exists().where(Run.idea_id == target.id)
    kept = exists().where(Candidate.run_id == Run.id, Run.idea_id == target.id, Candidate.verdict == Outcome.KEEP)
    ended = target.status.in_((IdeaStatus.PARKED, IdeaStatus.REJECTED)) | (
        (target.status == IdeaStatus.DONE) & ran & ~kept
    )
    query = (
        select(Idea, target)
        .join(link, (link.idea_id == Idea.id) & (link.kind == LinkKind.AFTER))
        .join(target, target.id == link.target_id)
        .where(
            Idea.campaign_id == campaign_id, Idea.status == IdeaStatus.READY, ~exists().where(Run.idea_id == Idea.id)
        )
        .where(ended)
        .order_by(Idea.id, target.id)
    )
    parked: dict[IdeaId, IdeaId] = {}
    causes: dict[IdeaId, Idea] = {}
    """The prerequisite at the start of each parked idea's chain: the idea that ended without a keep."""
    stranded: dict[Idea, list[tuple[Idea, Idea]]] = {}
    """Each such prerequisite, with the ideas that it parked and the prerequisite of each: one escalation for them."""
    while rows := s.execute(query).all():
        first: dict[Idea, Idea] = {}
        for idea, prerequisite in rows:
            first.setdefault(idea, prerequisite)
        for idea, prerequisite in first.items():
            idea.status = IdeaStatus.PARKED
            parked[idea.id] = prerequisite.id
            cause = causes[idea.id] = causes.get(prerequisite.id, prerequisite)
            stranded.setdefault(cause, []).append((idea, prerequisite))
        s.flush()
    for cause, ideas in stranded.items():
        escalations.escalate(s, TargetRef(IDEA, cause.id), _stranded(cause, ideas), author=System.DAEMON, priority=2)
    return parked


def _stranded(cause: Idea, ideas: list[tuple[Idea, Idea]]) -> EscalationView:
    """What the escalation asks of the ideas that ``cause`` parked, each with the prerequisite that it runs after."""
    outcome = 'kept nothing' if cause.status is IdeaStatus.DONE else f'is {cause.status}'
    match ideas:
        case [_]:
            waits, them, they, are, run, stay = 'An idea waits', 'it', 'It', 'it is', 'It runs', 'It stays'
        case _:
            waits, them, they, are = f'{len(ideas)} ideas wait', 'them', 'They', 'they are'
            run, stay = 'They run', 'They stay'
    unlinks = '; '.join(f'piloop idea unlink {idea.id} after {prerequisite.id}' for idea, prerequisite in ideas)
    return EscalationView(
        headline=f'{waits} for another idea that changed nothing. Do you run {them} anyway?',
        why=f'{they} must run after another idea, which {outcome}. So {are} parked.',
        choices=(
            Choice(id='A', label=f'Run {them} without that change.', result=f'{run} on the head.'),
            Choice(id='B', label='Wait for the other idea.', result=f'{stay} parked until that idea is ready.'),
        ),
        details=f'parked: {", ".join(f"idea#{idea.id}" for idea, _ in ideas)}, after idea#{cause.id}, which {outcome}.'
        f' Unlink each ({unlinks}) to run it without that change, or set them ready once idea#{cause.id} is ready'
        ' again',
    )


def supersede(s: Session, campaign_id: CampaignId) -> dict[IdeaId, IdeaId]:
    """Reject each idea of the campaign that a newer idea supersedes and that never ran.

    The daemon runs this on each pass. An idea never ran if it has no ``runs`` row and is ``seed``, ``proposed`` or
    ``ready``. A superseded idea that ran keeps its status and its candidates, even if a failed attempt put it back
    to ``ready``. The new idea is judged against those candidates. Research cannot change the status of other ideas.
    It inserts a superseding idea instead, and this function rejects the old one.

    The change-log trigger logs the status change (``idea.status``, actor ``daemon``). That event does not say which
    idea superseded it, so a ``note`` on the rejected idea says it.

    Returns:
        ``{rejected idea id: superseding idea id}``.
    """
    link = aliased(IdeaLink)
    superseded = (
        select(Idea)
        .where(
            Idea.campaign_id == campaign_id,
            Idea.status.in_(NEVER_RAN),
            ~exists().where(Run.idea_id == Idea.id),
            exists().where(link.target_id == Idea.id, link.kind == LinkKind.SUPERSEDES),
        )
        .order_by(Idea.id)
    )
    rejected: dict[IdeaId, IdeaId] = {}
    for idea in s.scalars(superseded):
        newest = select(func.max(IdeaLink.idea_id)).where(
            IdeaLink.target_id == idea.id, IdeaLink.kind == LinkKind.SUPERSEDES
        )
        by = s.scalar(newest)
        assert by is not None  # The EXISTS clause above found at least one such link.
        idea.status = IdeaStatus.REJECTED
        s.add(
            Comment(
                target=TargetRef(IDEA, idea.id),
                author=DAEMON_ACTOR,
                kind=CommentKind.NOTE,
                body=f'rejected: superseded by idea#{by}. It does not run, even if idea#{by} has not run yet.',
            )
        )
        rejected[idea.id] = by
    return rejected


def links(idea: Idea, superseded_by: dict[IdeaId, list[IdeaId]]) -> str:
    """Format the idea's links as one short cell, or ``—`` if there are none.

    The parts are ``blocked by idea#3 (rejected), idea#5 (ready)``, ``after idea#1`` (prerequisites that are done),
    ``supersedes idea#2, idea#4`` and ``superseded by idea#7``, joined with ``; ``. ``superseded_by`` maps an idea id
    to the ideas that supersede it. The caller computes it once for all the rows it renders.
    """
    parts: list[str] = []
    blocking = [t for t in idea.after if t.status is not IdeaStatus.DONE]
    done = [t for t in idea.after if t.status is IdeaStatus.DONE]
    if blocking:
        parts.append('blocked by ' + ', '.join(f'idea#{t.id} ({t.status})' for t in blocking))
    if done:
        parts.append('after ' + _refs(t.id for t in done))
    if idea.supersedes:
        parts.append('supersedes ' + _refs(t.id for t in idea.supersedes))
    if idea.id in superseded_by:
        parts.append('superseded by ' + _refs(superseded_by[idea.id]))
    return '; '.join(parts) or '—'


def _refs(ids: Iterable[int]) -> str:
    return ', '.join(f'idea#{i}' for i in ids)


def superseded_by(s: Session, idea_ids: list[IdeaId]) -> dict[IdeaId, list[IdeaId]]:
    """Map each of ``idea_ids`` that newer ideas supersede to the ids of those ideas, oldest first."""
    rows = s.execute(
        select(IdeaLink.target_id, IdeaLink.idea_id)
        .where(IdeaLink.target_id.in_(idea_ids), IdeaLink.kind == LinkKind.SUPERSEDES)
        .order_by(IdeaLink.idea_id)
    ).all()
    out: dict[IdeaId, list[IdeaId]] = {}
    for old, new in rows:
        out.setdefault(old, []).append(new)
    return out


def source(created_by: str) -> str:
    """Return where an idea came from: its signature without the agent id (``research#12`` → ``research``)."""
    return created_by.partition('#')[0]


@dataclass(frozen=True)
class Definition:
    """The parts that turn a seed into an idea that an implementer can run. The implementer reads each part in its brief."""

    hypothesis: str
    """The mechanism and why it should move the metric."""
    evidence: str
    """What supports it, with ids and paths: runs, findings, candidates, loss tables, documents."""
    check: str
    """What the evaluator should show if the idea works, and what would show it wrong. Never a gate before a commit."""
    metric: str
    """What decides success: the primary metric, or the part of it that the idea targets."""
    effect: str
    """The expected size of the effect in the metric's units, and why that much."""
    plan: str | None = None
    """More steps for the implementer."""

    def missing(self) -> list[str]:
        """Return the names of the required parts that are blank."""
        pieces = ('hypothesis', 'evidence', 'check', 'metric', 'effect')
        return [name for name in pieces if not getattr(self, name).strip()]


class NotASeed(Exception):
    """``develop`` got an idea that is no longer a seed. Another sweep or a human already took it."""


def develop(
    idea: Idea,
    definition: Definition,
    *,
    title: str | None = None,
    family: str | None = None,
    base: CandidateId | None = None,
    priority: int | None = None,
    proposed: bool = False,
) -> Idea:
    """Turn the seed ``idea`` into a defined idea. The caller commits.

    The idea becomes ``ready``, or ``proposed`` if a human must approve it. The original hunch and its pointers are
    kept. The definition goes first in the hypothesis. The plan starts with the check, the metric and the expected
    effect, so the implementer's brief has all of it. The change log records the connection's actor on the status change
    (``idea.status``). ``defined_by`` uses this to count a sweep's work.
    """
    if idea.status is not IdeaStatus.SEED:
        raise NotASeed(f'idea#{idea.id} is {idea.status}, not a seed: someone took it already')
    if gaps := definition.missing():
        raise ValueError(f'a defined idea needs {", ".join(gaps)}')
    idea.hypothesis = f'{definition.hypothesis.strip()}\n\nSeed ({idea.created_by}): {idea.hypothesis}'
    steps = [
        f'The evaluator should show: {definition.check.strip()}',
        f'Success: {definition.metric.strip()}; expected effect: {definition.effect.strip()}',
        *([definition.plan.strip()] if definition.plan and definition.plan.strip() else []),
    ]
    idea.plan = '\n'.join(steps)
    pointers = f'; seed pointers: {idea.provenance}' if idea.provenance else ''
    idea.provenance = f'{definition.evidence.strip()}{pointers}'
    idea.title = title or idea.title
    idea.family = family or idea.family
    idea.base_candidate_id = base if base is not None else idea.base_candidate_id
    idea.priority = priority if priority is not None else idea.priority
    idea.status = IdeaStatus.PROPOSED if proposed else IdeaStatus.READY
    return idea


def defined_by(s: Session, campaign_id: CampaignId) -> set[str]:
    """Return the signatures that added a defined idea to the campaign, by inserting it or by developing a seed.

    The agent that filed a seed did not define anything. A research sweep that did neither added no ideas, whatever
    else it wrote.
    """
    from_seed = (Event.target_kind == IDEA) & has(IdeaStatusChanged, old=IdeaStatus.SEED)
    was_seed = exists().where(from_seed, Event.target_id == Idea.id)
    inserted = select(Idea.created_by).where(Idea.campaign_id == campaign_id, Idea.status != IdeaStatus.SEED, ~was_seed)
    developed = (
        select(Event.actor)
        .join(Idea, Idea.id == Event.target_id)
        .where(
            from_seed,
            has(IdeaStatusChanged, new=(IdeaStatus.READY, IdeaStatus.PROPOSED)),
            Idea.campaign_id == campaign_id,
        )
    )
    return set(s.scalars(inserted)) | set(s.scalars(developed))


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


def idea_row(idea: Idea, newer: dict[IdeaId, list[IdeaId]]) -> IdeaRow:
    """``idea`` as a list of ideas answers it. ``newer`` is ``superseded_by`` of the ideas listed."""
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
        supersedes=[t.id for t in idea.supersedes],
        superseded_by=newer.get(idea.id, []),
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


def queued_ideas(s: Session, campaign: Campaign) -> tuple[Sequence[Idea], Sequence[Idea]]:
    """Return every ``ready`` idea in scheduling order, and the ones not blocked by a prerequisite."""
    mine = select(Idea).where(Idea.campaign_id == campaign.id).order_by(Idea.priority, Idea.id)
    waiting = s.scalars(mine.where(Idea.status == IdeaStatus.READY)).all()
    ready = s.scalars(mine.where(runnable())).all()
    return waiting, ready
