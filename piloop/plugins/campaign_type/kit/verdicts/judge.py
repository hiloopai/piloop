"""Judging: a measured candidate against its bar, the head and the keeps of each track, lineage and rebases."""

import re
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Final, Literal, assert_never

from sqlalchemy import Select, func, literal, select
from sqlalchemy.orm import Session

from piloop.actor import Actor, System
from piloop.campaign.campaigns import (
    repository,
)
from piloop.config import CampaignConfig
from piloop.db import requests
from piloop.db.events import emit
from piloop.db.models import (
    Campaign,
    Event,
    EventModel,
    ReadBy,
)
from piloop.ids import CampaignStatus, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.tables import Idea, IdeaLink
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Metric, Track
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId, TrackName
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    Samples,
    candidates_of,
    derive,
    full_clause,
    means,
    measured,
    metrics,
    pairs,
    ratios,
    requested,
    rungs,
    score,
    units,
)
from piloop.plugins.campaign_type.kit.evaluation.tables import Evaluator, Measurement
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.campaign_type.kit.verdicts.verdict import (
    judgeable,
    label,
    pending,
    pending_clause,
)
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import (
    VERDICT_RULES,
    Branch,
    Case,
    Criterion,
    Imported,
    Leave,
    Lineage,
    Moved,
    Node,
    Ratio,
    Rebase,
    Seed,
    Side,
    Stopped,
    Trunk,
    VerdictRule,
)
from piloop.plugins.host import ContractBroken
from piloop.plugins.vcs.contract import (
    Conflicts,
    RebaseVersion,
    Repository,
)
from piloop.text import NONE, metric


def head(s: Session, campaign: Campaign, *, excluding: Candidate | None = None) -> Candidate | None:
    """Return the head: the campaign's verdict rule chooses it among the trunk keeps (``VerdictRule.head``).

    Every run that pins no base starts from the head. Research and the watchdog get the head checked out, and
    ``target`` is read on it. ``excluding`` leaves out one candidate: the one being judged, which is never its own bar.

    Raises:
        ContractBroken: the rule chose a candidate that is no trunk keep.
    """
    trunk = [n for n in _trunk(s, campaign) if excluding is None or n.id != excluding.id]
    chosen = verdict_rule(campaign.config).head(trunk)
    return s.get_one(Candidate, _among(trunk, chosen, 'the head')) if chosen is not None else None


def last_keep_at(s: Session, campaign: Campaign) -> datetime | None:
    """When the run of the campaign's latest keep finished, or None before the first keep. The baseline is no keep."""
    return s.scalar(keeps(campaign).join(Run, Candidate.run_id == Run.id).with_only_columns(func.max(Run.finished_at)))


def trunk_keeps(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return the campaign's trunk keeps, oldest first: its keeps that raise the trunk's bar (``VerdictRule.raises_bar``)."""
    trunk = [n.id for n in _trunk(s, campaign)]
    return list(s.scalars(select(Candidate).where(Candidate.id.in_(trunk)).order_by(Candidate.id)))


def _trunk(s: Session, campaign: Campaign) -> list[Node]:
    """Return what the verdict rule reads of the trunk keeps, oldest first."""
    rule = verdict_rule(campaign.config)
    kept = keeps(campaign).with_only_columns(Candidate.id).order_by(Candidate.id)
    return [n for n in _nodes(s, kept, campaign.evaluator_id) if rule.raises_bar(n, Trunk())]


def verdict_rule(cfg: CampaignConfig) -> VerdictRule:
    """Return the campaign's verdict rule: ``[plugins.verdict_rule]``, else ``bootstrap``."""
    return VERDICT_RULES.selected(cfg.plugins)


def node(s: Session, candidate: Candidate) -> Node:
    """Return what the lineage methods of a verdict rule read of ``candidate``, with its means under the current evaluator."""
    evaluator_id = s.get_one(Campaign, candidate.campaign_id).evaluator_id
    [found] = _nodes(s, select(Candidate.id).where(Candidate.id == candidate.id), evaluator_id)
    return found


def _nodes(s: Session, ids: Select[CandidateId], evaluator_id: EvaluatorId | None) -> list[Node]:
    """Return what the lineage methods of a verdict rule read of the candidates of ``ids``, in its order.

    It reads columns, not rows: the trunk and an ancestry can hold thousands of candidates.
    """
    order = list(s.scalars(ids))
    given = ids.order_by(None)
    rows = select(Candidate.id, Candidate.verdict, Candidate.origin, Run.branch, Run.base_candidate_id)
    made = {
        c: (verdict, _made(origin, branch, base))
        for c, verdict, origin, branch, base in s.execute(
            rows.outerjoin(Run, Candidate.run_id == Run.id).where(Candidate.id.in_(given))
        )
    }
    measured = select(Measurement.candidate_id, Measurement.metrics).where(
        Measurement.candidate_id.in_(given),
        Measurement.evaluator_id == evaluator_id,
        full_clause(),
    )
    results: dict[CandidateId, list[dict[str, float]]] = {}
    for c, values in s.execute(measured.order_by(Measurement.id)):
        results.setdefault(c, []).append(values)
    found = ratios(s.get(Evaluator, evaluator_id) if evaluator_id is not None else None)
    return [Node(c, made[c][0], made[c][1], derive(means(results.get(c, [])), found)) for c in order]


def _made(origin: Origin, branch: bool | None, base: CandidateId | None) -> Lineage | Imported:
    """Return where a candidate was made: imported, on the branch of a branch run, else on the trunk."""
    match origin, branch, base:
        case Origin.REFERENCE, _, _:
            return Imported()
        case _, True, int() as root:
            return Branch(CandidateId(root))
        case _:
            return Trunk()


def _ancestry(candidate: Candidate) -> Select[CandidateId]:
    """Select the ids of ``candidate``, then its parent, and so on to its root."""
    up = select(Candidate.id, Candidate.parent_id, literal(0).label('depth')).where(Candidate.id == candidate.id)
    walk = up.cte('ancestry', recursive=True)
    walk = walk.union_all(
        select(Candidate.id, Candidate.parent_id, walk.c.depth + 1).join(walk, Candidate.id == walk.c.parent_id)
    )
    return select(walk.c.id).order_by(walk.c.depth)


def bar_of(s: Session, candidate: Candidate) -> Candidate:
    """Return the bar of a run that starts from ``candidate``: the campaign's verdict rule chooses it from the ancestry.

    Raises:
        ContractBroken: the rule chose no candidate of the ancestry.
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    line = _nodes(s, _ancestry(candidate), campaign.evaluator_id)
    chosen = verdict_rule(campaign.config).bar(line)
    return s.get_one(Candidate, _among(line, chosen, f'the bar of candidate#{candidate.id}'))


def _among(given: Sequence[Node], chosen: CandidateId, what: str) -> CandidateId:
    """Return ``chosen``, the id that a rule chose among ``given``.

    Raises:
        ContractBroken: the rule chose another id.
    """
    if chosen not in {n.id for n in given}:
        raise ContractBroken(f'the verdict rule chose candidate#{chosen} as {what}, which it was not given')
    return chosen


def track_of(cfg: CampaignConfig, candidate: Candidate) -> Track:
    """Return the track of the candidate's verdict, else of its run (its idea's track). A renderer shows its score on it.

    A candidate without either, such as the baseline or a reference, gets the first track (``fallback_track``).
    """
    run = candidate.run.idea.track if candidate.run is not None else None
    return fallback_track(cfg, candidate.track or run)


def fallback_track(cfg: CampaignConfig, name: TrackName | None) -> Track:
    """Return the track called ``name``, else the first track. An idea can name a track that ``piloop.toml`` removed."""
    return next((t for t in cfg.all_tracks if t.name == name), cfg.all_tracks[0])


def due(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return what the daemon measures with the current evaluator, most urgent first, each once.

    1. The head: an unpinned run starts from it and is judged against it, so the queue waits only for it
       (``head_unmeasured``).
    2. The bar of each open run (``bar_of`` its base), oldest run first: the run is judged against it.
    3. Each candidate that someone asked to measure again (``requested``), oldest request first, and each that waits
       for a verdict that no run gives (``unjudged``).
    4. The bar of each ``ready`` idea pinned to a base.
    5. Each reference that no evaluator measured: a new import.

    The head and the bars come only while the current evaluator has not measured them. Nothing else is measured again
    unasked: after an evaluator change, an older keep or a reference that no run needs keeps the scores of the old
    evaluator until someone asks (``request_remeasure``). A measurement never changes a keep's verdict. A stopped
    campaign gets only 3: what someone asked for, to review the campaign. The rest is for runs, and none starts.
    """
    open_runs = select(Run).join(Idea).where(Idea.campaign_id == campaign.id, Run.status.in_(OPEN_RUNS))
    pins = select(Candidate).join(Idea, Idea.base_candidate_id == Candidate.id)
    new_references = select(Candidate).where(
        Candidate.campaign_id == campaign.id, Candidate.origin == Origin.REFERENCE, ~Candidate.measurements.any()
    )
    waiting = select(Candidate).join(Run).where(Candidate.campaign_id == campaign.id, pending_clause())
    asked = [c for c, _ in requested(s, campaign)]
    active = campaign.status is CampaignStatus.ACTIVE
    found = (
        [head(s, campaign), *(bar_of(s, run.base) for run in s.scalars(open_runs.order_by(Run.id)))] if active else []
    )
    found += asked + [c for c in s.scalars(waiting.order_by(Candidate.id)) if unjudged(c)]
    if active:
        ready = (Idea.campaign_id == campaign.id, Idea.status == IdeaStatus.READY)
        found += [bar_of(s, c) for c in s.scalars(pins.where(*ready).order_by(Idea.id))]
        found += s.scalars(new_references.order_by(Candidate.id))
    evaluator_id = campaign.evaluator_id
    return [c for c in dict.fromkeys(found) if c is not None and (c in asked or not measured(c, evaluator_id))]


def head_unmeasured(s: Session, campaign: Campaign) -> bool:
    """Whether the head has no measurement from the current evaluator: the queue then holds the unpinned ideas."""
    trunk = head(s, campaign)
    return trunk is not None and not measured(trunk, campaign.evaluator_id)


class RejudgeRequested(EventModel):
    """Someone measured a candidate by hand (``piloop measurement add``): the daemon judges it again."""

    kind: Literal['candidate.rejudge_requested'] = 'candidate.rejudge_requested'
    measurement: MeasurementId


class RejudgeFailed(EventModel):
    """The daemon could not judge a candidate again. It answers the request."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['candidate.rejudge_failed'] = 'candidate.rejudge_failed'
    error: str


class CandidateJudged(EventModel):
    """The daemon judged a candidate on a track: its verdict, the score of the track's primary and the reason.

    A candidate is judged on each track whose primary it has a value for, one event each. The last is the track of the
    verdict that the candidate has (``candidates.track``).
    """

    kind: Literal['candidate.judged'] = 'candidate.judged'
    verdict: Outcome
    score: float | None
    reason: str | None
    track: TrackName | None = None
    """The track. Null before the daemon judged on every track."""


def unjudged(candidate: Candidate) -> bool:
    """Return whether ``candidate`` waits for a verdict that no run gives: pending, and its run scores no more.

    That is the merge onto the head of a keep that someone set off the head (``ops.set_verdict``). The daemon measures
    it (``due``) and judges it against the head that it merged onto (``RunController.remeasure``).
    """
    run = candidate.run
    return pending(candidate) and run is not None and run.status not in OPEN_RUNS and not run.scoring


def rejudges(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return the candidates of ``campaign`` that someone asked to judge again (``RejudgeRequested``), oldest first.

    The daemon answers each request when it judges the candidate again, or finds that it cannot (``answer_rejudge``).
    """
    return [
        s.get_one(Candidate, asked.target_id)
        for asked, _ in requests.pending(s, RejudgeRequested, candidates_of(campaign))
    ]


def answer_rejudge(s: Session, candidate_id: CandidateId) -> None:
    """Answer the pending request to judge the candidate again: the daemon judged it, or found that it cannot."""
    for asked, _ in requests.pending(
        s, RejudgeRequested, Event.target == TargetRef(TargetKind.CANDIDATE, candidate_id)
    ):
        requests.answer(s, asked)


def keeps(campaign: Campaign) -> Select[Candidate]:
    """Select the campaign's kept candidates. The baseline is one while its verdict is ``keep``."""
    return select(Candidate).where(Candidate.campaign_id == campaign.id, Candidate.verdict == Outcome.KEEP)


def baseline(s: Session, campaign: Campaign) -> Candidate | None:
    """Return the campaign's first candidate: the repo's HEAD when the campaign was registered.

    None before it has one: a campaign added by hand, with SQL.
    """
    return s.scalars(_first(campaign)).first()


def _first(campaign: Campaign) -> Select[Candidate]:
    return select(Candidate).where(Candidate.campaign_id == campaign.id).order_by(Candidate.id).limit(1)


def checkout_candidate(s: Session, campaign: Campaign) -> Candidate:
    """Return what a sweeping agent gets checked out: the head, or the baseline when its verdict was overridden.

    Raises:
        NoResultFound: The campaign has no candidate yet (``baseline``).
    """
    return head(s, campaign) or s.scalars(_first(campaign)).one()


def stop_of(candidate: Candidate, evaluator_id: EvaluatorId) -> Stopped | None:
    """Where the candidate's latest climb under ``evaluator_id`` stopped, as a verdict rule reads it, or None."""
    for m in reversed(candidate.measurements):
        if m.evaluator_id == evaluator_id and m.rung is not None and m.stopped is not None:
            return Stopped(m.rung, m.stopped)
    return None


def judge(
    candidate: Candidate,
    own: Candidate,
    evaluator_id: EvaluatorId,
    cfg: CampaignConfig,
    track: Track,
    units_dir: Path,
    *,
    head: Candidate | None,
    side: Lineage,
) -> tuple[Outcome, str]:
    """Return the daemon's verdict on ``candidate``, a candidate of ``track``, against ``own``, and the reason.

    ``own`` is the run's bar: its last keep, or its last candidate that kept against its bar and was rebased. It
    starts as the run's base. The run controller keeps a run's candidate on the trunk only while ``own`` is the head. A keep
    against an ``own`` that the head has moved past is rebased onto the head instead (``rebase``).

    On a ``Branch`` (``runs.branch``: its idea pinned another starting point), a run keeps what beats ``own``: a branch
    keep. Its reason also says whether it is above ``head`` on the track's primary.

    The campaign's verdict rule gives the verdict (``decide``): ``[plugins.verdict_rule]``, else ``bootstrap``. It
    reads the candidate's parent too.
    """
    mine = metrics(candidate, evaluator_id)
    mine_samples, theirs_samples = pairs(candidate, own, evaluator_id)

    def as_side(c: Candidate, samples: Samples | None = None) -> Side:
        return Side(
            f'candidate#{c.id}',
            metrics(c, evaluator_id),
            units(c, evaluator_id, units_dir),
            samples or {},
            stopped=stop_of(c, evaluator_id),
            rungs=rungs(c, evaluator_id),
        )

    parent = candidate.parent
    verdict, reason = decide(
        verdict_rule(cfg),
        track,
        as_side(candidate, mine_samples),
        as_side(own, theirs_samples),
        parent=as_side(parent) if parent is not None else None,
    )
    primary = track.primary
    leader = metrics(head, evaluator_id).get(primary.name) if head is not None else None
    match side, verdict:
        # a rule from a plugin may keep a candidate that has no primary value
        case Branch(), Outcome.KEEP if head is not None and leader is not None and primary.name in mine:
            where = 'above' if primary.better(mine[primary.name], leader) else 'not above'
            reason += f'; a branch keep, {where} the head candidate#{head.id} ({primary.name} {metric(leader)})'
        case _:
            pass
    return verdict, reason


@dataclass(frozen=True)
class OnTrack:
    """The daemon's verdict on a candidate on one track, and the reason."""

    track: Track
    verdict: Outcome
    reason: str


RANK: Final = (Outcome.KEEP, Outcome.INTERESTING, Outcome.DISCARD, Outcome.CRASH)
"""The verdicts, best first: a candidate has the best of its tracks' verdicts."""


def judge_tracks(
    candidate: Candidate,
    own: Candidate,
    evaluator_id: EvaluatorId,
    cfg: CampaignConfig,
    track: Track,
    units_dir: Path,
    *,
    head: Candidate | None,
    side: Lineage,
) -> list[OnTrack]:
    """Return the verdicts on ``candidate`` against ``own`` (``judge``) on each track, the best last.

    It is judged on ``track``, its run's, then on each other track whose primary it has a value for. The best is the
    first of ``RANK``, the earliest on a tie. The bar and the measurements are the same on each track: only which metric is the primary and which are the
    guardrails differs. A reason of another track than ``track`` says which.
    """
    mine = metrics(candidate, evaluator_id)
    others = [t for t in cfg.all_tracks if t.name != track.name and t.primary.name in mine]
    judged = [
        OnTrack(t, *judge(candidate, own, evaluator_id, cfg, t, units_dir, head=head, side=side))
        for t in (track, *others)
    ]
    best = next(j for verdict in RANK for j in judged if j.verdict is verdict)
    shown = best if best is judged[0] else replace(best, reason=f'on the {best.track.name} track: {best.reason}')
    return [*(j for j in judged if j is not best), shown]


def criterion(m: Metric) -> Criterion:
    """Return what a verdict rule reads of a metric of ``piloop.toml``."""
    ratio = None if m.ratio is None else Ratio(m.ratio.numerator, m.ratio.denominator)
    return Criterion(m.name, m.direction, m.margin, m.floor, m.fraction, ratio)


def decide(
    rule: VerdictRule, track: Track, candidate: Side, bar: Side, *, parent: Side | None = None
) -> tuple[Outcome, str]:
    """Return ``rule``'s verdict on ``candidate``, a candidate of ``track``, against ``bar``, and the reason.

    The rule gets the case as data (``piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract.Case``): the track's primary and guardrails,
    and the results of both sides and of the candidate's ``parent``.
    """
    guardrails = tuple(map(criterion, track.guardrails))
    judged = rule.decide(Case(criterion(track.primary), guardrails, candidate, bar, parent))
    return judged.verdict, judged.reason


class Rebased(EventModel):
    """A candidate that kept against its own bar after the head moved on was merged onto the head.

    Only one of ``rebased``, ``known`` and ``conflicts`` is set.
    """

    kind: Literal['candidate.rebased'] = 'candidate.rebased'
    head: CandidateId
    """The head that it was merged onto."""
    rebased: CandidateId | None = None
    """The merged candidate, which is measured and judged against that head next."""
    known: CandidateId | None = None
    """The candidate that the merged tree is already."""
    conflicts: list[str] | None = None
    """The paths that conflict."""


REBASED_SUFFIX = re.compile(r' \(rebased onto candidate#\d+\)$')
"""How the subject of a merge commit ends. A merge that is rebased again keeps the run commit's subject, with the
new head."""


MOVED = 'keeps against its own bar, but the head moved on to candidate#{head}: '
"""How the reason of a candidate to rebase starts. The candidate is ``interesting`` (``Cause.MOVED``), never a keep off
the trunk."""


def on_moved(
    s: Session, cfg: CampaignConfig, repo: Repository, candidate: Candidate, onto: Candidate
) -> Rebased | None:
    """Do what the campaign's verdict rule says follows ``candidate``, a keep that lacks the head ``onto``.

    ``candidate`` is ``interesting`` (``Cause.MOVED``) already. Return how it was merged onto the head (``rebase``), or
    None when the rule leaves it.
    """
    match verdict_rule(cfg).on_moved(Moved(node(s, candidate), node(s, onto))):
        case Rebase(conflicts=conflicts):
            return rebase(s, repo, candidate, onto, conflicts)
        case Leave():
            return None
        case never:
            assert_never(never)


def rebase(s: Session, repo: Repository, candidate: Candidate, onto: Candidate, conflicts: Seed | Leave) -> Rebased:
    """Merge ``candidate``'s version onto the head ``onto``. Return how: the merged candidate to measure and judge, if any.

    ``candidate`` kept against its own bar after the head moved on. So it is ``interesting`` (``Cause.MOVED``), and its
    reason ends with the result of the merge. The merge is a version made without a checkout (``Repository.merge``),
    and kept (``RebaseVersion``). A new tree becomes a pending
    candidate of the same run, with ``onto`` as its parent. A merge that conflicts files a seed on the track of its verdict to
    compose the two by hand when ``conflicts`` is ``Seed``, unless the campaign stopped. The seed is not pinned, so it runs from the head
    (``follow_up``). It supersedes the compose seeds of the run's earlier candidates that ``candidate`` contains
    (``supersede_composed``). A merged tree that is already a candidate (the head had the change) gives no new
    candidate. Emits ``Rebased``. The ideas that wait for the run's idea wait for the seed instead
    (``wait_for_compose``).
    """
    campaign = s.get_one(Campaign, candidate.campaign_id)
    subject = REBASED_SUFFIX.sub('', candidate.summary or '')
    message = f'{subject} (rebased onto candidate#{onto.id})'
    merged = repo.merge(onto.revision, candidate.revision, message)
    if isinstance(merged, Conflicts):
        paths = ', '.join(merged.paths)
        why = (
            f'it kept against its own bar but its merge onto the head candidate#{onto.id} conflicts in {paths}:'
            f' from the head, merge candidate#{candidate.id} ({candidate.revision}) and resolve the conflicts'
        )
        assert candidate.run is not None  # only a run's candidate is rebased
        stopped = campaign.status is CampaignStatus.STOPPED
        match conflicts:
            case Seed() if not stopped:
                title = f'Compose candidate#{candidate.id} onto the head'
                on = track_of(campaign.config, candidate).name  # the track that kept it
                seeds = follow_up(s, candidate, [title], why, System.DAEMON, track=on, pin=False)
            case Seed() | Leave():
                seeds = []
            case _:
                assert_never(conflicts)
        for seed in seeds:
            supersede_composed(s, repo, candidate, seed)
            wait_for_compose(s, candidate.run.idea, seed)
        filed = f': seed idea#{seeds[0].id} composes it by hand' if seeds else ''
        candidate.verdict_reason = f'{candidate.verdict_reason}; its merge onto the head conflicts in {paths}{filed}'
        rebased = Rebased(head=onto.id, conflicts=list(merged.paths))
    else:
        repo.keep(merged.revision, RebaseVersion(campaign.name, candidate.id))
        found, created = resolve(
            s, candidate.campaign_id, merged, parent=onto, run_id=candidate.run_id, origin=Origin.RUN
        )
        if created:
            candidate.verdict_reason = f'{candidate.verdict_reason}; rebased onto the head as candidate#{found.id}'
            rebased = Rebased(head=onto.id, rebased=found.id)
        else:
            candidate.verdict_reason = (
                f'{candidate.verdict_reason}; its merge onto the head is candidate#{found.id} already ({label(found)})'
            )
            rebased = Rebased(head=onto.id, known=found.id)
    emit(s, rebased, TargetRef(TargetKind.CANDIDATE, candidate.id), System.DAEMON)
    return rebased


def supersede_composed(s: Session, repo: Repository, candidate: Candidate, seed: Idea) -> None:
    """Link ``seed`` as superseding each compose seed of the run's earlier candidates that ``candidate`` contains.

    Composing ``candidate`` composes those too, so ``ideas.supersede`` rejects each one that is still a seed. A run of
    four chained commits that each conflict on the head then leaves one seed, not four.
    """
    earlier = select(Candidate).where(Candidate.run_id == candidate.run_id, Candidate.id != candidate.id)
    by_provenance = {str(TargetRef(TargetKind.CANDIDATE, c.id)): c for c in s.scalars(earlier)}
    composes = select(Idea).where(
        Idea.campaign_id == candidate.campaign_id,
        Idea.status == IdeaStatus.SEED,
        Idea.created_by == System.DAEMON,
        Idea.provenance.in_(by_provenance),
    )
    for old in s.scalars(composes):
        assert old.provenance is not None  # the IN clause matched it
        if repo.contains(candidate.revision, by_provenance[old.provenance].revision):
            s.add(IdeaLink(idea_id=seed.id, target_id=old.id, kind=LinkKind.SUPERSEDES))


NEVER_RAN = (IdeaStatus.SEED, IdeaStatus.PROPOSED, IdeaStatus.READY)
"""The statuses of an idea that has not started: the daemon can reject it when it is superseded and has no run
(``ideas.supersede``), and move its ``after`` links (``wait_for_compose``).

A failed attempt puts an idea back to ``ready`` if it has attempts left. The daemon does not cancel such an idea."""


def wait_for_compose(s: Session, idea: Idea, seed: Idea) -> None:
    """Move the ``after`` links to ``idea`` of the ideas that have not started to ``seed``, the compose of its change.

    Its run's change did not land on the trunk: the merge conflicted. They would start from a head without it, or
    be parked as stranded once ``idea`` is done. The change lands through the compose now, so they wait for it. A
    started idea keeps its link: it began without the change. The link triggers log the move.
    """
    links = select(IdeaLink).join(Idea, Idea.id == IdeaLink.idea_id)
    waiting = links.where(IdeaLink.target_id == idea.id, IdeaLink.kind == LinkKind.AFTER, Idea.status.in_(NEVER_RAN))
    for link in s.scalars(waiting).all():
        s.delete(link)
        s.add(IdeaLink(idea_id=link.idea_id, target_id=seed.id, kind=LinkKind.AFTER))
    s.flush()


def reproposed(candidate: Candidate, run: Run) -> bool:
    """Return whether ``run``'s commit proposes the known ``candidate`` again, so that the run judges it again.

    It is a ``judgeable`` candidate of another run on the same side of the trunk (``runs.branch``). Its verdict came
    from another bar, or from a merge onto the head that conflicted. A compose that reproduces the tree must be able to
    land. A run on the other side never judges it: a branch run's keep of a trunk run's candidate would move the head,
    and a trunk run's keep of a branch run's candidate never would.
    """
    other = candidate.run
    return other is not None and other.id != run.id and other.branch == run.branch and judgeable(candidate)


def follow_up(
    s: Session,
    candidate: Candidate,
    titles: Sequence[str],
    why: str,
    actor: Actor,
    *,
    track: TrackName,
    pin: bool = True,
) -> list[Idea]:
    """File one seed per title on ``track``, pinned to ``candidate`` unless ``pin`` is False. Return the new seeds.

    A title that the campaign already has gets `` (from candidate#<id>)``. A follow-up already filed from this candidate
    with that title, under either name and in any status, is not filed again: a rejected compose seed stays rejected
    when the candidate is judged again. These are seeds, not ready ideas: research develops them before they run. A
    pinned one runs as a branch run when its candidate is not the computed base (``[plugins.role.implementer] explore_every``). An
    unpinned one runs from the head, as a trunk run.
    """
    ideas: list[Idea] = []
    hypothesis = f'Follow-up of candidate#{candidate.id} ({candidate.summary or "no summary"})'
    provenance = f'candidate#{candidate.id}'
    for title in titles:
        names = (title, f'{title} (from {provenance})')
        taken = s.scalars(select(Idea).where(Idea.campaign_id == candidate.campaign_id, Idea.title.in_(names))).all()
        name = next((n for n in names if n not in {i.title for i in taken}), None)
        if name is None or any(i.provenance == provenance for i in taken):
            continue
        idea = Idea(
            campaign_id=candidate.campaign_id,
            title=name,
            hypothesis=f'{hypothesis}: {why}',
            status=IdeaStatus.SEED,
            track=track,
            base_candidate_id=candidate.id if pin else None,
            provenance=provenance,
            created_by=actor.signature,
        )
        s.add(idea)
        s.flush()
        ideas.append(idea)
    return ideas


def side(run: Run) -> Lineage:
    """Return where ``run`` keeps: on a branch from its base, or on the trunk."""
    return Branch(run.base_candidate_id) if run.branch else Trunk()


def lineage(candidate: Candidate) -> Lineage:
    """Return the lineage of ``candidate``: the branch of the nearest branch run among its ancestors, else the trunk.

    A branch from the baseline is a different lineage from the trunk that grew from it.
    """
    while (run := candidate.run) is not None:
        if run.branch:
            return Branch(run.base_candidate_id)
        candidate = run.base
    return Trunk()


@dataclass(frozen=True)
class Line:
    """One lineage of keeps, with its latest keep."""

    lineage: Lineage
    latest: Candidate
    keeps: int


def lineages(s: Session, campaign: Campaign) -> list[Line]:
    """Return every lineage of keeps: the trunk first (its latest keep is the head), then the branches, newest first."""
    found: dict[Lineage, Line] = {}
    for c in s.scalars(keeps(campaign).order_by(Candidate.id)):
        at = lineage(c)
        known = found.get(at)
        found[at] = Line(at, c, (known.keeps if known else 0) + 1)
    return sorted(found.values(), key=lambda line: (isinstance(line.lineage, Branch), -line.latest.id))


def branches(s: Session, campaign: Campaign, pinned: Candidate | None, computed: Candidate | None) -> bool:
    """Return True if a run on ``pinned`` is a branch run: the pinned base's bar is not the current head.

    A pin whose bar is the head, such as a non-keep on top of it, is not a branch. Its run is judged against the head
    (``ledger.bar_of``). Nor is a pin whose version has the head's (``Repository.contains``), such as a compose that
    cherry-picked it: a keep of it builds on the head.
    """
    if pinned is None or computed is None:
        return pinned is not None
    if bar_of(s, pinned).id == computed.id:
        return False
    with closing(repository(campaign)) as repo:
        return not repo.contains(pinned.revision, computed.revision)


def head_scores(
    s: Session, campaign: Campaign, cfg: CampaignConfig
) -> tuple[Candidate, list[tuple[Track, float | None, float | None]]] | None:
    """Return the head and, for each track, its primary score and its delta against the baseline.

    A score is None when the current evaluator has not measured it. A delta is also None when the head is the
    baseline. Returns None when there is no trunk keep.
    """
    trunk = head(s, campaign)
    if trunk is None:
        return None
    evaluator_id = campaign.evaluator_id
    first = baseline(s, campaign)
    values: list[tuple[Track, float | None, float | None]] = []
    for track in cfg.all_tracks:
        value = score(trunk, evaluator_id, track.primary)
        base = score(first, evaluator_id, track.primary) if first is not None and first.id != trunk.id else None
        values.append((track, value, None if value is None or base is None else value - base))
    return trunk, values


def scored(c: Candidate, evaluator_id: EvaluatorId | None, primary: Metric) -> tuple[str, str]:
    """Format the primary score of the candidate and its delta to its parent, each ``—`` when unknown."""
    value = score(c, evaluator_id, primary)
    parent = score(c.parent, evaluator_id, primary) if c.parent is not None else None
    delta = metric(value - parent, signed=True) if value is not None and parent is not None else NONE
    return NONE if value is None else metric(value), delta
