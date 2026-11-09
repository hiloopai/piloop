"""Submissions: the versions that an implementer sends to be scored while it works (``piloop submit``).

A submission is a ``run.submitted`` event on its run. The daemon scores each one at once into the pool
(``RunController.score_submission``), while the implementer works on: where it stands reaches the implementer as a
message (``tell_scores``). A run may submit ``[evaluator] submissions`` versions.
"""

from typing import ClassVar, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor, System
from piloop.agents.results import queue_prompt
from piloop.campaign.ops import Refused
from piloop.db.events import emit, in_order, latest
from piloop.db.models import AgentSession, Campaign, Event, EventModel, ReadBy
from piloop.db.session import write_transaction
from piloop.ids import CommentId, Revision, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import Measured
from piloop.plugins.campaign_type.kit.evaluation.measure import remeasure_asked, request_remeasure, status
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, latest_agent_session, run_of
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


def submitted(s: Session, run_id: RunId) -> list[CandidateId]:
    """The candidates that ``run_id`` submitted, oldest first."""
    return [found.candidate for _, found in in_order(s, Submitted, Event.target == TargetRef(RUN, run_id))]


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
    """The submission of ``run`` that is ``version``'s tree, if the run submitted it: a retry, answered as it is."""
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    return known if known is not None and known.id in submitted(s, run.id) else None


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
        for event, made in in_order(s, Submitted, Event.target == on_run):
            found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, made.candidate))
            if found is None or found[0].id < event.id or found[0].id in sent:
                continue
            row, scored = found
            line = scored.told if len(scored.told) <= chars else scored.told[: chars - 1] + '…'
            text = TOLD.format(candidate=made.candidate, revision=made.revision[:12], told=line)
            if (comment := queue_prompt(s, agent.id, text, System.DAEMON)) is None:
                break  # its worker is not running
            emit(s, ScoreTold(candidate=made.candidate, scored=row.id), on_run, System.DAEMON)
            sent.add(row.id)
            told.append(comment)
    return told


def refuse(s: Session, run: Run) -> None:
    """Refuse a new submission of ``run`` before anything of it is taken in.

    Raises:
        SubmitRefused: the run is not running or has claimed no idea, or it used the submissions that the campaign
            caps it to.
    """
    campaign = run.campaign
    cfg = campaign.config.evaluator
    if run.status is not RunStatus.RUNNING:
        raise SubmitRefused(f'run#{run.id} is {run.status}, not running')
    if run.idea_id is None:  # scored on no track: its version would stand for no idea
        raise SubmitRefused(f'run#{run.id} has claimed no idea: `piloop idea claim` or `piloop idea propose` first')
    if cfg.submissions is not None and len(submitted(s, run.id)) >= cfg.submissions:
        raise SubmitRefused(f'run#{run.id} used its {cfg.submissions} submissions')


def submit(s: Session, run: Run, version: Version, repo: Repository, actor: Actor) -> Candidate:
    """Submit ``version`` of ``run``: its candidate, a child of the run's last submission, else of its base.

    Emits ``Submitted``, and keeps the version (``r<run>/s<n>``). The caller commits. A tree that the run submitted
    already is that submission again (``resubmitted``): nothing new, no submission spent, whatever the run's state
    (``retried``).

    Raises:
        SubmitRefused: ``refuse`` refuses it, or the version's tree is a candidate already.
    """
    write_transaction(s)  # the count of its submissions and the new one in one write: two at once never pass the cap
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    if (own := resubmitted(s, run, version)) is not None:
        retried(s, own, actor)
        return own
    refuse(s, run)
    made = submitted(s, run.id)
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    if known is not None and known.origin not in (Origin.RECOVERED, Origin.SKIPPED):
        raise SubmitRefused(f'its tree is candidate#{known.id} already ({known.origin}): change something first')
    parent = s.get_one(Candidate, made[-1]) if made else run.base
    candidate, _ = resolve(s, run.campaign_id, version, parent=parent, run_id=run.id, origin=Origin.RUN)
    candidate.origin = Origin.RUN
    repo.keep(version.revision, Pin(run.campaign.name, f'r{run.id}/s{len(made)}'))
    emit(s, Submitted(candidate=candidate.id, revision=version.revision), TargetRef(RUN, run.id), actor)
    return candidate


def waiting(s: Session, campaign: Campaign) -> list[Candidate]:
    """The submitted candidates of ``campaign`` that wait for their score, oldest first (``unscored``)."""
    runs = select(Run.id).where(Run.campaign_id == campaign.id)
    found = in_order(s, Submitted, Event.target_kind == RUN, Event.target_id.in_(runs))
    last = {made.candidate: event.id for event, made in found}  # a later submission of a tree is the one to answer
    return [s.get_one(Candidate, c) for c, since in last.items() if unscored(s, c, since)]


def unscored(s: Session, candidate: CandidateId, since: int) -> bool:
    """Whether ``candidate`` waits for a score: no ``candidate.scored`` after the event ``since``.

    One whose evaluation someone cancelled under the current evaluator waits for none (``Measured.CANCELLED``).
    """
    found = s.get_one(Candidate, candidate)
    if status(found, s.get_one(Campaign, found.campaign_id).evaluator_id).cancelled:
        return False
    scored = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate))
    return scored is None or scored[0].id < since
