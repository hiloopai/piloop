"""The trunk of an ``accumulating`` campaign: the work that it accepted, and the steps that merge more onto it.

The trunk is the newest accepted merge: the candidate of the campaign's newest ``trunk.advanced`` event, else its
baseline (``head``). Nothing else stores it. A submission waits until a merge decides it: merged into the trunk, or
refused. Its decision is a ``candidate.scored`` event on it, whose line its worker hears (``tell_scores``), and which
settles its run (``submissions.unscored``). The kit does not score a submission of its own: only the merge is
measured.

A merge takes two steps, with its measurement in between, as ``tests/models/accumulating.tla`` models them:

1. ``start``: merge each waiting submission in turn onto the trunk. One that conflicts with the trunk or with an earlier
   one is refused at once. After a failed batch of several, its submissions go one at a time (``RetryAlone``).
2. ``decide``: accept the merge when its evaluation scored and its primary is at least the trunk's, and the trunk did
   not move since ``start``; else a batch of several is tried again one at a time, and one alone is refused.
"""

from dataclasses import dataclass
from typing import ClassVar, Literal

from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.db.events import emit, in_order, latest
from piloop.db.models import Campaign, Event, EventModel, ReadBy
from piloop.db.session import write_transaction
from piloop.ids import CampaignId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import submissions
from piloop.plugins.campaign_type.kit.evaluation.measure import score
from piloop.plugins.campaign_type.kit.pool.pool import baseline, track_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.vcs.contract import Conflict, Pin, Repository, Version


class TrunkAdvanced(EventModel):
    """A merge of submissions onto the trunk was accepted: it is the trunk now."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['trunk.advanced'] = 'trunk.advanced'
    candidate: CandidateId
    """The merge: the new trunk."""
    base: CandidateId
    """The trunk that it merged onto."""
    merged: tuple[CandidateId, ...]
    """The submissions that it merged."""


class RetryAlone(EventModel):
    """A submission was in a batch whose merge failed: it is merged again on its own, so that only its own fault counts."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['trunk.retry_alone'] = 'trunk.retry_alone'
    merge: CandidateId
    """The merge that failed."""


@dataclass(frozen=True)
class Batch:
    """A merge that ``start`` made and its measurement waits for: what ``decide`` decides."""

    campaign: CampaignId
    base: CandidateId
    """The trunk when it started."""
    merge: CandidateId
    members: tuple[CandidateId, ...]
    """The submissions that it merged, in order."""


def _on_campaign(campaign: Campaign) -> TargetRef:
    return TargetRef(TargetKind.CAMPAIGN, campaign.id)


def head(s: Session, campaign: Campaign) -> Candidate | None:
    """The trunk: the newest accepted merge, else the baseline. None before the campaign has a baseline."""
    found = latest(s, TrunkAdvanced, Event.target == _on_campaign(campaign))
    return baseline(s, campaign) if found is None else s.get_one(Candidate, found[1].candidate)


def merged(s: Session, campaign: Campaign) -> set[CandidateId]:
    """The submissions that the trunk holds."""
    return {c for _, done in in_order(s, TrunkAdvanced, Event.target == _on_campaign(campaign)) for c in done.merged}


def answers(s: Session, submission: Candidate, version: Version) -> bool:
    """Whether the answer to ``submission`` answers ``version``, a version of its tree that its run submits again.

    A merge is of one version onto one trunk. Another version merges otherwise: one that merged the trunk into its work
    and kept its own files has the same tree, and now merges cleanly. A trunk that moved since a refusal is another
    merge too. Only a merge into the trunk answers every version of its tree: the trunk holds it.
    """
    campaign = s.get_one(Campaign, submission.campaign_id)
    if submission.id in merged(s, campaign):
        return True
    if version.revision != submission.revision:
        return False
    if submission.id in {c.id for c in submissions.waiting(s, campaign)}:
        return True
    decided, trunk = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, submission.id)), head(s, campaign)
    return decided is None or (trunk is not None and decided[1].best == trunk.id)


def alone(s: Session, candidate: Candidate) -> bool:
    """Whether ``candidate`` waits to be merged on its own: a batch of it failed since its last decision."""
    retry = latest(s, RetryAlone, Event.target == TargetRef(CANDIDATE, candidate.id))
    decided = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate.id))
    return retry is not None and (decided is None or decided[0].id < retry[0].id)


def start(s: Session, repo: Repository, campaign: Campaign) -> Batch | None:
    """Merge the waiting submissions onto the trunk, refuse those that conflict, and return the merge to measure.

    Every submission that waits, oldest first, or the oldest one that waits to go alone (``alone``). None when nothing
    is left to measure: nothing waits, or each one was refused. The caller commits the refusals.
    """
    trunk = head(s, campaign)
    pending = submissions.waiting(s, campaign)
    if trunk is None or not pending:
        return None
    solo = [c for c in pending if alone(s, c)]
    members: list[Candidate] = []
    at: Version | None = None
    for candidate in solo[:1] or pending:
        made = repo.merge(trunk.revision if at is None else at.revision, candidate.revision, _summary(candidate))
        if isinstance(made, Conflict):
            refuse(s, candidate, trunk, f'it conflicts with the trunk in {", ".join(made.paths)}: {made.detail}')
        else:
            at = made
            members.append(candidate)
    if at is None:
        return None
    merge, _ = resolve(s, campaign.id, at, parent=trunk, run_id=None, origin=Origin.MERGE)
    if merge.id == trunk.id:
        for candidate in members:
            refuse(s, candidate, trunk, 'the trunk has its work already')
        return None
    repo.keep(at.revision, Pin(campaign.name, f'merge/{merge.id}'))
    return Batch(campaign.id, trunk.id, merge.id, tuple(c.id for c in members))


def _summary(candidate: Candidate) -> str:
    run = f' of run#{candidate.run_id}' if candidate.run_id is not None else ''
    return f'merge candidate#{candidate.id}{run} into the trunk'


def decide(s: Session, batch: Batch) -> bool:
    """Decide ``batch`` once its merge is measured, in one write transaction, and return whether the trunk moved.

    The trunk takes the merge when the merge scored and its primary is at least the trunk's, and the trunk is still the
    batch's base: a merge that started on an older trunk is not taken, and its submissions wait to be merged onto the
    new one. A batch of several that is not taken goes again one submission at a time. One alone is refused. The caller
    commits.
    """
    write_transaction(s)
    campaign = s.get_one(Campaign, batch.campaign)
    trunk = head(s, campaign)
    if trunk is None or trunk.id != batch.base:
        return False
    merge = s.get_one(Candidate, batch.merge)
    primary = campaign.config.all_tracks[0].primary
    ours, theirs = score(merge, campaign.evaluator_id, primary), score(trunk, campaign.evaluator_id, primary)
    members = [s.get_one(Candidate, c) for c in batch.members]
    if ours is not None and (theirs is None or not primary.better(theirs, ours)):
        emit(
            s,
            TrunkAdvanced(candidate=merge.id, base=trunk.id, merged=batch.members),
            _on_campaign(campaign),
            System.DAEMON,
        )
        for candidate in members:
            decided(s, candidate, merge, f'merged into the trunk: candidate#{merge.id}, {primary.name} {ours:g}')
        return True
    if len(members) > 1:
        for candidate in members:
            emit(s, RetryAlone(merge=merge.id), TargetRef(CANDIDATE, candidate.id), System.DAEMON)
        return False
    why = (
        f'the merge onto the trunk (candidate#{merge.id}) scored no {primary.name}'
        if ours is None
        else f'the merge onto the trunk (candidate#{merge.id}) has {primary.name} {ours:g}, worse than the trunk {theirs:g}'
    )
    refuse(s, members[0], trunk, why)
    return False


def refuse(s: Session, candidate: Candidate, trunk: Candidate, why: str) -> None:
    """Refuse a submission: its worker hears the trunk to merge into its work before it submits again, and why.

    What to do comes first: ``tell_scores`` cuts a long line, and only the end of ``why`` (git's output) may go.
    """
    told = f'not merged: merge the trunk ({trunk.revision}) into your work, then submit again. Why: {why}'
    decided(s, candidate, trunk, told)


def decided(s: Session, candidate: Candidate, trunk: Candidate, told: str) -> None:
    """Record the decision on a submission: a ``candidate.scored`` with its worker's line, and ``trunk`` as the best."""
    cfg = s.get_one(Campaign, candidate.campaign_id).config
    scored = CandidateScored(
        track=track_of(cfg, candidate).name, score=None, measured=0, best=trunk.id, rank=None, told=told
    )
    emit(s, scored, TargetRef(CANDIDATE, candidate.id), System.DAEMON)
