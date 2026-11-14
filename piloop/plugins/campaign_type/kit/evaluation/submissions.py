"""Submissions: the versions that an implementer sends to be scored while it works (``piloop submit``).

A submission is a ``run.submitted`` event on its run, for one of the run's claims, and its row in ``submissions``,
which names the claim's idea: one writer (``record``) writes both in one transaction. A tree submitted again for
another claim is the same candidate, and one row more. Which ideas a candidate was submitted for, by which run, is
``submissions_of``: settling a claim, the history and the yields read it. The daemon scores each one at once into the
pool (``RunController.score_submission``), while the implementer works on: where it stands reaches the implementer as
a message (``tell_scores``). A run may submit ``[evaluator] submissions`` versions.
"""

from collections.abc import Collection
from typing import ClassVar, Literal

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from piloop.actor import Actor, System
from piloop.agents.results import queue_prompt
from piloop.campaign.ops import Refused
from piloop.db.events import emit, in_order, latest, payload_of
from piloop.db.models import AgentSession, Campaign, Event, EventModel, ReadBy, among
from piloop.db.session import write_transaction
from piloop.ids import CampaignId, CommentId, Revision, TargetRef
from piloop.plugins.campaign_type.contract import CAMPAIGN_TYPES
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import Measured
from piloop.plugins.campaign_type.kit.evaluation.measure import remeasure_asked, request_remeasure, status
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import (
    Run,
    RunClaim,
    Submission,
    latest_agent_session,
    newest_claim,
    run_of,
)
from piloop.plugins.vcs.contract import Pin, Repository, Version


class Submitted(EventModel):
    """An implementer submitted a version of its run: the daemon measures and judges its candidate now."""

    kind: Literal['run.submitted'] = 'run.submitted'
    candidate: CandidateId
    revision: Revision


class ScoreTold(EventModel):
    """The daemon told the run's worker where its submission ``candidate`` stands: the record ``scored``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.score_told'] = 'run.score_told'
    candidate: CandidateId
    scored: int
    """The id of the ``CandidateScored`` event that the message told."""


EventModel.retire('run.verdict_told')  # 0.5 told a worker the verdict of its submission; 0.6 tells its standing

TOLD = '[piloop] candidate#{candidate} ({revision}): {told}'
"""The message that tells a worker where its submission stands."""


class SubmitRefused(Refused):
    """``submit`` refuses the version. The message says why, and what to do."""


def record(s: Session, run_id: RunId, candidate: CandidateId, revision: Revision, idea: IdeaId, actor: Actor) -> None:
    """Record that ``run_id`` submitted ``candidate`` for its claim of ``idea``, its event and its row. The caller commits.

    The one writer of both: what the feed says was submitted and what ``submissions_of`` reads never part.
    """
    event = emit(s, Submitted(candidate=candidate, revision=revision), TargetRef(RUN, run_id), actor)
    s.flush()
    s.add(Submission(run_id=run_id, candidate_id=candidate, idea_id=idea, event_id=event.id))


def submissions_of(
    s: Session,
    *,
    campaign: CampaignId | None = None,
    runs: Collection[RunId] | None = None,
    ideas: Collection[IdeaId] | None = None,
    candidates: Collection[CandidateId] | None = None,
) -> list[Submission]:
    """The submissions that match each filter that is given, oldest first: one query, whatever the number of each.

    Every reader of which ideas a candidate was submitted for reads it. ``campaign`` filters in SQL, and the ids go as
    one parameter (``among``), so no number of runs or candidates reaches SQLite's limit on parameters.
    """
    where: list[ColumnElement[bool]] = []
    if runs is not None:
        where.append(among(Submission.run_id, runs))
    if ideas is not None:
        where.append(among(Submission.idea_id, ideas))
    if candidates is not None:
        where.append(among(Submission.candidate_id, candidates))
    if campaign is not None:
        where.append(Submission.run_id.in_(select(Run.id).where(Run.campaign_id == campaign)))
    return list(s.scalars(select(Submission).where(*where).order_by(Submission.id)))


def submitted(s: Session, run_id: RunId) -> list[CandidateId]:
    """The candidates that ``run_id`` submitted, oldest first."""
    return [found.candidate_id for found in submissions_of(s, runs=[run_id])]


def submitted_for(s: Session, claim: RunClaim) -> list[CandidateId]:
    """The candidates that the run of ``claim`` submitted for it, oldest first, each once."""
    found = submissions_of(s, runs=[claim.run_id], ideas=[claim.idea_id])
    return list(dict.fromkeys(f.candidate_id for f in found))


def ideas_of(s: Session, candidates: Collection[CandidateId]) -> dict[CandidateId, set[IdeaId]]:
    """The ideas of each of ``candidates``, in two queries, whatever their number.

    Those of the claims that it was submitted for, else, for a version that no run submitted for a claim, the idea that
    it was made for (``candidates.idea_id``).
    """
    found: dict[CandidateId, set[IdeaId]] = {c: set() for c in candidates}
    for sent in submissions_of(s, candidates=found):
        if sent.idea_id is not None:
            found[sent.candidate_id].add(sent.idea_id)
    unsent = [c for c, ideas in found.items() if not ideas]
    for candidate, idea in s.execute(select(Candidate.id, Candidate.idea_id).where(among(Candidate.id, unsent))):
        if idea is not None:
            found[candidate].add(idea)
    return found


def run_of_agent(s: Session, actor: Actor) -> Run:
    """The run that ``actor``, an implementer, works on or worked on, whatever its state: a retry may come after its end.

    Raises:
        SubmitRefused: the actor is no agent of a run.
    """
    agent = actor.launched
    session = s.get(AgentSession, agent.session) if agent is not None else None
    if session is None or (run_id := run_of(session)) is None:
        raise SubmitRefused(f'{actor} has no run: only a worker submits')
    return s.get_one(Run, run_id)


def resubmitted(s: Session, run: Run, version: Version) -> Candidate | None:
    """The submission of ``run`` that is ``version``'s tree, if its answer answers ``version``: a retry, answered as it is.

    The campaign's type says what its answer answers (``CampaignType.answers``).
    """
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    if known is None or known.id not in submitted(s, run.id):
        return None
    return known if CAMPAIGN_TYPES.model_of(run.campaign.type)().answers(s, known, version) else None


def retried(s: Session, own: Candidate, actor: Actor) -> None:
    """A resubmit of the run's own submission: ask once for one more evaluation when it crashed (``Measured.CRASHED``).

    Load can push an evaluation past the evaluate timeout. A version that scored keeps its score: to evaluate it again,
    ``piloop candidate remeasure``. The caller commits.
    """
    current = s.get_one(Campaign, own.campaign_id).evaluator_id
    if status(own, current).state is Measured.CRASHED and remeasure_asked(s, own) is None:
        request_remeasure(s, own, actor, repeats=1)


def tell_scores(s: Session, campaign: Campaign, chars: int) -> list[CommentId]:
    """Message each running worker of ``campaign`` where each of its submissions stands, once per score. The caller commits.

    A score before the run submitted the candidate is not news to it. The line is cut at ``chars``
    (``[agents] steer_line_chars``). The worker reads it at its next ``piloop`` call (``deliver``).
    """
    told = []
    running = select(Run).where(Run.campaign_id == campaign.id, Run.status == RunStatus.RUNNING)
    for run in s.scalars(running):
        agent, on_run = latest_agent_session(s, run.id), TargetRef(RUN, run.id)
        if agent is None:
            continue
        sent = {made.scored for _, made in in_order(s, ScoreTold, Event.target == on_run)}
        newest = {made.candidate_id: made for made in submissions_of(s, runs=[run.id])}
        for made in newest.values():  # a later submission of a tree is the one that its answer answers
            found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, made.candidate_id))
            if found is None or found[0].id < made.event_id or found[0].id in sent:
                continue
            row, scored = found
            revision = payload_of(s.get_one(Event, made.event_id), Submitted).revision
            line = scored.told if len(scored.told) <= chars else scored.told[: chars - 1] + '…'
            text = TOLD.format(candidate=made.candidate_id, revision=revision[:12], told=line)
            if (comment := queue_prompt(s, agent.id, text, System.DAEMON)) is None:
                break  # its worker is not running
            emit(s, ScoreTold(candidate=made.candidate_id, scored=row.id), on_run, System.DAEMON)
            sent.add(row.id)
            told.append(comment)
    return told


def refuse(s: Session, run: Run, idea: IdeaId | None) -> RunClaim:
    """Refuse a new submission of ``run`` before anything of it is taken in, else return the claim that it is for.

    That is the claim of ``idea``, by default the run's newest (``newest_claim``).

    Raises:
        SubmitRefused: the run is not running, it has no claim of ``idea`` or none at all, or it used the submissions
            that the campaign caps it to.
    """
    campaign = run.campaign
    cfg = campaign.config.evaluator
    if run.status is not RunStatus.RUNNING:
        raise SubmitRefused(f'run#{run.id} is {run.status}, not running')
    claim = newest_claim(run) if idea is None else next((c for c in run.claims if c.idea_id == idea), None)
    if claim is None:  # scored on no track: its version would stand for no idea
        mine = ', '.join(f'idea#{c.idea_id}' for c in run.claims)
        raise SubmitRefused(
            f'run#{run.id} has no claim of idea#{idea}: it claimed {mine}'
            if idea is not None and mine
            else f'run#{run.id} has claimed no idea: `piloop idea claim` or `piloop idea propose` first'
        )
    if cfg.submissions is not None and len(submitted(s, run.id)) >= cfg.submissions:
        raise SubmitRefused(f'run#{run.id} used its {cfg.submissions} submissions')
    return claim


def submit(
    s: Session, run: Run, version: Version, repo: Repository, actor: Actor, idea: IdeaId | None = None
) -> Candidate:
    """Submit ``version`` of ``run`` for its claim of ``idea`` (``refuse``): a child of its last submission, else of its base.

    The submission names the claim's idea (``record``), and so does the candidate, as the claim that it was submitted
    for last.

    Records it (``record``), and keeps the version (``r<run>/s<n>``). The caller commits. A tree that the run submitted
    already, whose answer answers this version, is that submission again (``resubmitted``): nothing new, no submission
    spent, whatever the run's state (``retried``), and its claim stays. One whose answer does not is submitted again,
    as this version. A tree that a merge onto an ``accumulating`` campaign's trunk made is submitted as that merge,
    whose measurement stands.

    Raises:
        SubmitRefused: ``refuse`` refuses it, or the version's tree is a candidate already.
    """
    write_transaction(s)  # the count of its submissions and the new one in one write: two at once never pass the cap
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    if (own := resubmitted(s, run, version)) is not None:
        retried(s, own, actor)
        return own
    claim = refuse(s, run, idea)
    made = submitted(s, run.id)
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    if (
        known is not None
        and known.id not in made
        and known.origin not in (Origin.RECOVERED, Origin.SKIPPED, Origin.MERGE)
    ):
        raise SubmitRefused(f'its tree is candidate#{known.id} already ({known.origin}): change something first')
    parent = s.get_one(Candidate, made[-1]) if made else run.base
    candidate, _ = resolve(s, run.campaign_id, version, parent=parent, run_id=run.id, origin=Origin.RUN)
    if candidate.origin is not Origin.MERGE:  # a worker that merged the trunk into its work can reach a merge's tree
        candidate.origin = Origin.RUN
    candidate.revision = version.revision  # a merge onto a trunk takes the history of the version submitted last
    candidate.idea_id = claim.idea_id
    repo.keep(version.revision, Pin(run.campaign.name, f'r{run.id}/s{len(made)}'))
    record(s, run.id, candidate.id, version.revision, claim.idea_id, actor)
    return candidate


def waiting(s: Session, campaign: Campaign) -> list[Candidate]:
    """The submitted candidates of ``campaign`` that wait for their score, oldest first (``unscored``)."""
    found = submissions_of(s, campaign=campaign.id)
    last = {made.candidate_id: made.event_id for made in found}  # a later submission of a tree is the one to answer
    return [s.get_one(Candidate, c) for c, since in last.items() if unscored(s, c, since)]


def all_scored(s: Session, run_id: RunId) -> bool:
    """Whether no submission of ``run_id`` waits for its score (``waiting``): its claims can settle."""
    return {c.id for c in waiting(s, s.get_one(Run, run_id).campaign)}.isdisjoint(submitted(s, run_id))


def unscored(s: Session, candidate: CandidateId, since: int) -> bool:
    """Whether ``candidate`` waits for a score: no ``candidate.scored`` after the event ``since``.

    One whose evaluation someone cancelled under the current evaluator waits for none (``Measured.CANCELLED``).
    """
    found = s.get_one(Candidate, candidate)
    if status(found, s.get_one(Campaign, found.campaign_id).evaluator_id).cancelled:
        return False
    scored = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate))
    return scored is None or scored[0].id < since
