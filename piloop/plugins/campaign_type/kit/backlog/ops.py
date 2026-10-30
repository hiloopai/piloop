"""The operations on the backlog that the API and the CLI run: ideas, claims, links, tracks and statuses."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.agents.roles import files_of
from piloop.campaign.ops import Refused, existing, required_reason
from piloop.db.models import AgentSession, Campaign, Comment
from piloop.db.session import write_lock
from piloop.ids import CampaignId, CommentKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ideas import holders
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.role.contract import Files


def files(campaign: Campaign, actor: Actor) -> None:
    """Refuse an idea from ``actor`` when its role files none (``[plugins.role.<name>] files``).

    Raises:
        Refused: the actor's role files nothing.
    """
    if files_of(campaign.config, actor) is Files.NOTHING:
        raise Refused(
            f"{actor} files no ideas for others: a worker tries one of its own with `piloop idea propose '<title>'`"
        )


def check_plan(campaign: Campaign, actor: Actor, plan: str | None) -> None:
    """Refuse a plan longer than ``plan_words`` of the actor's role. An actor that is not a sweep has no limit.

    Raises:
        Refused: the plan is too long.
    """
    if (
        plan is None
        or (agent := actor.launched) is None
        or (table := campaign.config.roles.sweeping().get(agent.role)) is None
    ):
        return
    if (words := len(plan.split())) > table.plan_words:
        raise Refused(
            f'the plan has {words} words, and a plan of a {agent.role} has at most {table.plan_words}'
            ' (`plan_words`): give the steps of one run, and let the evaluator judge the result'
        )


def add_idea(
    s: Session,
    campaign_id: CampaignId,
    title: str,
    *,
    actor: Actor,
    hypothesis: str = '',
    plan: str | None = None,
    provenance: str | None = None,
    priority: int = 2,
    track: TrackName | None = None,
    after: Sequence[IdeaId] = (),
) -> Idea:
    """Add an ``open`` idea, with its links, and return it.

    The idea and its links are written in the caller's transaction, so the daemon never sees the idea without them.
    Emits ``idea.created``.

    Raises:
        Refused: the actor files nothing, the track is not the campaign's, or a link is refused (``link``).
    """
    campaign = existing(s, Campaign, campaign_id, 'campaign')
    files(campaign, actor)
    check_plan(campaign, actor, plan)
    return _added(s, campaign, title, actor, hypothesis, plan, provenance, priority, track, after)


def _added(
    s: Session,
    campaign: Campaign,
    title: str,
    actor: Actor,
    hypothesis: str,
    plan: str | None,
    provenance: str | None,
    priority: int,
    track: TrackName | None,
    after: Sequence[IdeaId],
) -> Idea:
    idea = Idea(
        campaign_id=campaign.id,
        track=track_for(campaign, track),
        title=title,
        hypothesis=hypothesis or title,
        plan=plan,
        provenance=provenance,
        priority=priority,
        status=IdeaStatus.OPEN,
        created_by=actor.signature,
    )
    s.add(idea)
    s.flush()
    for target in after:
        _link(s, idea, LinkKind.AFTER, target)
    return idea


def run_of_actor(s: Session, actor: Actor) -> Run:
    """The open run that ``actor``, a worker, works on: queued or running.

    Raises:
        Refused: the actor is no agent of a run, or its run ended.
    """
    agent = actor.launched
    session = s.get(AgentSession, agent.session) if agent is not None else None
    run_id = run_of(session) if session is not None else None
    if run_id is None:
        raise Refused(f'{actor} has no run: only a worker claims an idea')
    run = s.get_one(Run, run_id)
    if run.status not in OPEN_RUNS:
        raise Refused(f'run#{run.id} is {run.status}: a run claims while it is open')
    return run


def claim(s: Session, idea_id: IdeaId, *, actor: Actor) -> Idea:
    """Claim the open idea for the actor's run: the run works on it, and the idea is ``claimed``. The caller commits.

    A run claims one idea, and the claim ends with the run (``release``). The checks and the write are one write
    transaction, so two runs never claim one idea, and no claim lands after its run ended. The ``idea.status`` event,
    the trigger's, is signed by the worker.

    Raises:
        Refused: ``run_of_actor`` refuses, or ``_claim`` does.
    """
    write_lock(s)
    return _claim(s, run_of_actor(s, actor), idea_id)


def _claim(s: Session, run: Run, idea_id: IdeaId) -> Idea:
    """Claim ``idea_id`` for ``run``, under the write lock that the caller took.

    Raises:
        Refused: the run claimed an idea already, or the idea is missing, in another campaign or not open, its track
            is not in ``piloop.toml``, or an ``after`` link holds it (``holders``).
    """
    idea = existing(s, Idea, idea_id, 'idea')
    if run.idea_id is not None:
        raise Refused(f'run#{run.id} claimed idea#{run.idea_id} already: a run tries one idea')
    if idea.campaign_id != run.campaign_id:
        raise Refused(f'idea#{idea.id} is in another campaign than run#{run.id}')
    if idea.status is not IdeaStatus.OPEN:
        raise Refused(f'idea#{idea.id} is {idea.status}: claim an open idea')
    if holding := holders(idea):
        raise Refused(f'idea#{idea.id} is held by {", ".join(f"idea#{t.id} ({t.status})" for t in holding)}')
    if idea.track not in (tracks := [t.name for t in idea.campaign.config.all_tracks]):
        raise Refused(
            f'idea#{idea.id} is on track {idea.track!r}, which piloop.toml does not have: {", ".join(tracks)}'
        )
    attempt = len(idea.runs) + 1
    run.idea, run.attempt, idea.status = idea, attempt, IdeaStatus.CLAIMED
    s.flush()
    return idea


def propose(
    s: Session,
    title: str,
    *,
    actor: Actor,
    hypothesis: str = '',
    plan: str | None = None,
    provenance: str | None = None,
    track: TrackName | None = None,
) -> Idea:
    """Add an open idea to the board of the actor's run and claim it for the run, in one write transaction.

    A worker's own idea: it files no idea for others (``files``), but it may try one of its own.

    Raises:
        Refused: ``run_of_actor`` or ``_claim`` refuses, or the track is not the campaign's.
    """
    write_lock(s)
    run = run_of_actor(s, actor)
    idea = _added(s, run.campaign, title, actor, hypothesis, plan, provenance, 2, track, ())
    return _claim(s, run, idea.id)


SETTABLE = (IdeaStatus.OPEN, IdeaStatus.DROPPED)
"""The statuses that ``set_idea_status`` sets. ``claimed`` and ``done`` are a run's: a done idea has the result of
its run."""


def open_run(idea: Idea) -> Run | None:
    """Return the idea's run that is queued, running or being scored, or None.

    While it is open, the daemon still owns the idea: it settles the idea's status when the run's commits are judged,
    and it judges them on the idea's track. So only dropping the idea is safe meanwhile.
    """
    return next((r for r in idea.runs if r.status in OPEN_RUNS or r.scoring), None)


def check_status(idea: Idea, status: IdeaStatus) -> None:
    """Refuse a status that ``set_idea_status`` would refuse, before a client asks for the reason.

    Raises:
        Refused: ``status`` is the idea's already, or not one of ``SETTABLE``. An idea that is claimed or has an open
            run (``open_run``) can only be dropped: ``open`` would let a second run claim it.
    """
    if idea.status is status:
        raise Refused(f'idea#{idea.id} is {status} already')
    if status not in SETTABLE:
        raise Refused(f'{status} is not set by hand: give {", ".join(SETTABLE)}')
    if status is not IdeaStatus.DROPPED and (busy := _busy(idea)) is not None:
        raise Refused(f'idea#{idea.id} {busy}; drop it, or wait for the run to end')


def _busy(idea: Idea) -> str | None:
    """Say why the daemon still owns the idea (``open_run``), or None."""
    if (run := open_run(idea)) is not None:
        return f'has an open run: run#{run.id} is {"being scored" if run.scoring else run.status}'
    if idea.status is IdeaStatus.CLAIMED:
        return 'is claimed: its run is open'
    return None


def set_idea_status(s: Session, idea_id: IdeaId, status: IdeaStatus, *, reason: str, actor: Actor) -> IdeaStatus:
    """Set the idea's status, with a ``note`` on it that says why, and return the status it had.

    The ``idea.status`` event is the trigger's. Dropping an idea with an open run lets the run finish; the daemon then
    leaves the idea as it is.

    Raises:
        Refused: the reason is blank, or ``check_status`` refuses.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    why = required_reason(reason)
    check_status(idea, status)
    old = idea.status
    _note(s, idea, f'{old} → {status}: {why}', actor)
    idea.status = status
    return old


def link(s: Session, idea_id: IdeaId, kind: LinkKind, target_id: IdeaId, *, reason: str, actor: Actor) -> None:
    """Link the idea to ``target``: it waits until ``target`` is done, with a ``note`` that says why.

    The link is written in the caller's transaction, so a worker claims the idea before it or never in between.
    The ``idea.linked`` event is the trigger's.

    Raises:
        Refused: the reason is blank, or ``_link`` refuses.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    why = required_reason(reason)
    _link(s, idea, kind, target_id)
    _note(s, idea, f'{kind} idea#{target_id}: {why}', actor)


def _link(s: Session, idea: Idea, kind: LinkKind, target_id: IdeaId) -> None:
    """Add the link from ``idea``: every way to link ideas goes through here.

    Raises:
        Refused: the target is missing, the two are one idea or in two campaigns, the link exists, the idea is
            claimed or has an open run (an ``after`` link comes too late for its run), or the link closes a cycle.
    """
    target = existing(s, Idea, target_id, 'idea')
    if idea.id == target.id:
        raise Refused(f'idea#{idea.id} cannot link to itself')
    if idea.campaign_id != target.campaign_id:
        raise Refused(f'idea#{target.id} is in another campaign than idea#{idea.id}')
    if any(lk.kind is kind and lk.target_id == target.id for lk in idea.links):
        raise Refused(f'idea#{idea.id} links {kind} idea#{target.id} already')
    if kind is LinkKind.AFTER and (busy := _busy(idea)) is not None:
        raise Refused(f'idea#{idea.id} {busy}: it started without idea#{target.id}')
    if _reaches(s, target.id, idea.id, kind):
        raise Refused(f'a cycle: idea#{target.id} already links {kind} idea#{idea.id}, through its {kind} links')
    idea.links.append(IdeaLink(target_id=target.id, kind=kind))
    s.flush()


def _reaches(s: Session, start: IdeaId, goal: IdeaId, kind: LinkKind) -> bool:
    """Return whether ``kind`` links lead from idea ``start`` to idea ``goal``."""
    seen, todo = {start}, [start]
    while todo:
        step = select(IdeaLink.target_id).where(IdeaLink.idea_id.in_(todo), IdeaLink.kind == kind)
        todo = [t for t in s.scalars(step) if t not in seen]
        if goal in todo:
            return True
        seen.update(todo)
    return False


def unlink(s: Session, idea_id: IdeaId, kind: LinkKind, target_id: IdeaId, *, reason: str, actor: Actor) -> None:
    """Delete the idea's ``kind`` link to ``target``, with a ``note`` that says why. The trigger logs ``idea.unlinked``.

    Raises:
        Refused: the reason is blank, the idea is missing, or it has no such link.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    why = required_reason(reason)
    found = next((lk for lk in idea.links if lk.kind is kind and lk.target_id == target_id), None)
    if found is None:
        raise Refused(f'idea#{idea.id} has no {kind} link to idea#{target_id}')
    idea.links.remove(found)
    _note(s, idea, f'no longer {kind} idea#{target_id}: {why}', actor)


def set_track(s: Session, idea_id: IdeaId, track: TrackName, *, reason: str, actor: Actor) -> TrackName:
    """Move the idea to another track of its campaign, with a ``note`` that says why, and return the old track.

    The ``idea.track`` event is the trigger's.

    Raises:
        Refused: the reason is blank, the idea is missing, the track is not in ``piloop.toml``, it is the idea's track,
            or the idea is scheduled or has an open run: the run's commits are judged on the idea's track.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    why = required_reason(reason)
    track = track_for(idea.campaign, track)
    if idea.track == track:
        raise Refused(f'idea#{idea.id} is on track {track} already')
    if (busy := _busy(idea)) is not None:
        raise Refused(f'idea#{idea.id} {busy}: its commits are judged on track {idea.track}')
    old = idea.track
    _note(s, idea, f'track {old} → {track}: {why}', actor)
    idea.track = track
    return old


def set_priority(s: Session, idea_id: IdeaId, priority: int) -> int:
    """Set the idea's priority, from 0 (urgent) to 4 (someday), and return the old one.

    The ``idea.priority`` event is the trigger's.

    Raises:
        Refused: the idea is missing.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    old, idea.priority = idea.priority, priority
    return old


def track_for(campaign: Campaign, track: TrackName | None) -> TrackName:
    """Return the track for an idea: ``track`` when ``piloop.toml`` has it, else the only track.

    Raises:
        Refused: no track is given and the campaign has several, or the campaign has no such track.
    """
    tracks = [t.name for t in campaign.config.all_tracks]
    if track is None and len(tracks) == 1:
        return tracks[0]
    if track is None or track not in tracks:
        given = 'name one with --track' if track is None else f'there is no track {track!r}'
        raise Refused(f'{campaign.name} has tracks {", ".join(tracks)}: {given}')
    return track


def _note(s: Session, idea: Idea, body: str, actor: Actor) -> None:
    s.add(Comment(target=TargetRef(IDEA, idea.id), author=actor.signature, kind=CommentKind.NOTE, body=body))
