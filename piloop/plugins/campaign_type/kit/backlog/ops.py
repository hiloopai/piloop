"""The operations on the backlog that the API and the CLI run: ideas, links, bases, tracks and statuses."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.agents.roles import files_of
from piloop.campaign.ops import Refused, existing, required_reason
from piloop.db.models import Campaign, Comment
from piloop.ids import CampaignId, CommentKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.role.contract import Files


def files(campaign: Campaign, actor: Actor) -> None:
    """Refuse an idea from ``actor`` when its role files none (``[plugins.role.<name>] files``).

    Raises:
        Refused: the actor's role files nothing.
    """
    if files_of(campaign.config, actor) is Files.NOTHING:
        raise Refused(f"{actor} files no ideas: say the hunch in a note on your run's idea, with pointers.")


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
    harness: str | None = None,
    base: CandidateId | None = None,
    track: TrackName | None = None,
    after: Sequence[IdeaId] = (),
) -> Idea:
    """Add an ``open`` idea, with its links, and return it.

    The idea and its links are written in the caller's transaction, so the daemon never sees the idea without them.
    Emits ``idea.created``.

    Raises:
        Refused: the actor files nothing, the base or the track is not the campaign's, or a link is refused (``link``).
    """
    campaign = existing(s, Campaign, campaign_id, 'campaign')
    files(campaign, actor)
    check_base(s, campaign, base)
    check_plan(campaign, actor, plan)
    idea = Idea(
        campaign_id=campaign.id,
        track=track_for(campaign, track),
        title=title,
        hypothesis=hypothesis or title,
        plan=plan,
        provenance=provenance,
        priority=priority,
        harness=harness,
        base_candidate_id=base,
        status=IdeaStatus.OPEN,
        created_by=actor.signature,
    )
    s.add(idea)
    s.flush()
    for target in after:
        _link(s, idea, LinkKind.AFTER, target)
    return idea


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


def set_base(
    s: Session, idea_id: IdeaId, candidate_id: CandidateId | None, *, reason: str, actor: Actor
) -> CandidateId | None:
    """Pin the idea to start from ``candidate_id``, or clear the pin with None, and return the old base.

    A cleared pin runs from the head at the time the run starts. A run already open keeps its base, and the next
    attempt takes the new one. A ``note`` says why; the ``idea.base_candidate_id`` event is the trigger's.

    Raises:
        Refused: the reason is blank, the idea is missing, the candidate is not one of its campaign, or it is the base.
    """
    idea = existing(s, Idea, idea_id, 'idea')
    why = required_reason(reason)
    check_base(s, idea.campaign, candidate_id)
    old = idea.base_candidate_id
    if old == candidate_id:
        raise Refused(f'idea#{idea.id} starts from {_base(candidate_id)} already')
    _note(s, idea, f'base {_base(old)} → {_base(candidate_id)}: {why}', actor)
    idea.base_candidate_id = candidate_id
    return old


def _base(candidate_id: CandidateId | None) -> str:
    return 'the head' if candidate_id is None else f'candidate#{candidate_id}'


def check_base(s: Session, campaign: Campaign, candidate_id: CandidateId | None) -> None:
    """Refuse a base that is not unset or a candidate of ``campaign``.

    Raises:
        Refused: no such candidate in the campaign.
    """
    if candidate_id is None:
        return
    if s.scalar(select(Candidate.campaign_id).where(Candidate.id == candidate_id)) != campaign.id:
        raise Refused(f'no candidate#{candidate_id} in campaign {campaign.name}')


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
