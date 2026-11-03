"""Submissions: the versions that an implementer sends to be scored while it works (``piloop submit``).

A submission is a ``run.submitted`` event on its run. The daemon measures and judges each one at once, against the
head (``RunController.judge_submission``), while the implementer works on: its verdict reaches the implementer as a
message (``tell_verdicts``). A run may submit ``[evaluator] submissions`` versions.
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
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin, Outcome
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.campaign_type.kit.evaluation.evaluate import TIMED_OUT
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics, remeasure_asked, request_remeasure
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, latest_agent_session, run_of
from piloop.plugins.campaign_type.kit.verdicts.judge import CandidateJudged, criterion
from piloop.plugins.campaign_type.kit.verdicts.verdict import label, pending, rejudgeable
from piloop.plugins.vcs.contract import Pin, Repository, Version
from piloop.text import metric


class Submitted(EventModel):
    """An implementer submitted a version of its run: the daemon measures and judges its candidate now."""

    kind: Literal['run.submitted'] = 'run.submitted'
    candidate: CandidateId
    revision: Revision


class VerdictTold(EventModel):
    """The daemon told the run's worker the verdict of its submission ``candidate``: the judgement ``judged``."""

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['run.verdict_told'] = 'run.verdict_told'
    candidate: CandidateId
    judged: int
    """The id of the ``CandidateJudged`` event that the message told."""


VERDICT = '[piloop] candidate#{candidate} ({revision}): {verdict}{score} — {reason}'
"""The message that tells a worker the verdict of its submission."""


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


def limited(s: Session, candidate: Candidate) -> bool:
    """Whether a limit that load can push a version past gave the daemon's verdict on ``candidate``.

    That is a crash because the evaluation ran out of time, or a discard because a metric is past its floor, as a CPU
    time is past its limit on a busy machine. Such a verdict is not final: a resubmit of the tree measures it again
    (``remeasure``). An evaluation that failed for the machine records nothing, and runs again by itself.
    """
    if not rejudgeable(candidate) or candidate.verdict not in (Outcome.CRASH, Outcome.DISCARD):
        return False
    campaign = s.get_one(Campaign, candidate.campaign_id)
    mine = [m for m in candidate.measurements if m.evaluator_id == campaign.evaluator_id]
    if any(m.error is not None and m.error.startswith(TIMED_OUT) for m in mine):
        return True
    means = metrics(candidate, campaign.evaluator_id)
    return any(m.name in means and criterion(m).past_floor(means[m.name]) for m in campaign.config.metrics)


def remeasure(s: Session, candidate: Candidate, actor: Actor) -> bool:
    """Ask once to measure ``candidate`` again when its verdict is ``limited``, and return whether it is measured again.

    The daemon then judges it on all its measurements (``RunController.remeasure``), and its run's worker hears the
    new verdict. The caller commits.
    """
    if not limited(s, candidate):
        return False
    if remeasure_asked(s, candidate) is None:
        request_remeasure(s, candidate, actor, repeats=1)
    return True


def tell_verdicts(s: Session, campaign: Campaign, chars: int) -> list[CommentId]:
    """Message each running worker of ``campaign`` each new judgement of its submissions, once. The caller commits.

    A judgement before the run submitted the candidate is not news to it. The reason is cut at ``chars``
    (``[agents] steer_line_chars``). A worker whose harness reads messages only between turns can read the verdict with
    the SQL tool that its brief names.
    """
    told = []
    running = select(Run).where(Run.campaign_id == campaign.id, Run.status == RunStatus.RUNNING)
    for run in s.scalars(running):
        agent, on_run = latest_agent_session(s, run.id), TargetRef(RUN, run.id)
        if agent is None:
            continue
        sent = {made.judged for _, made in in_order(s, VerdictTold, Event.target == on_run)}
        for event, made in in_order(s, Submitted, Event.target == on_run):
            found = latest(s, CandidateJudged, Event.target == TargetRef(CANDIDATE, made.candidate))
            if found is None or found[0].id < event.id or found[0].id in sent:
                continue
            judged, verdict = found
            reason = verdict.reason or ''
            text = VERDICT.format(
                candidate=made.candidate,
                revision=made.revision[:12],
                verdict=verdict.verdict,
                score='' if verdict.score is None else f' {metric(verdict.score)}',
                reason=reason if len(reason) <= chars else reason[: chars - 1] + '…',
            )
            if (comment := queue_prompt(s, agent.id, text, System.DAEMON)) is None:
                break  # its worker is not running
            emit(s, VerdictTold(candidate=made.candidate, judged=judged.id), on_run, System.DAEMON)
            sent.add(judged.id)
            told.append(comment)
    return told


def refuse(s: Session, run: Run) -> None:
    """Refuse a new submission of ``run`` before anything of it is taken in.

    Raises:
        SubmitRefused: the campaign scores its runs after they end, the run is not running or has claimed no idea, or
            it used the submissions that the campaign caps it to.
    """
    campaign = run.campaign
    cfg = campaign.config.evaluator
    if cfg.scoring is not Scoring.SUBMIT:
        raise SubmitRefused(f'campaign {campaign.name} scores every commit after its run ends: commit, and end the run')
    if run.status is not RunStatus.RUNNING:
        raise SubmitRefused(f'run#{run.id} is {run.status}, not running')
    if run.idea_id is None:  # judged on no track, its version could move the head for no idea
        raise SubmitRefused(f'run#{run.id} has claimed no idea: `piloop idea claim` or `piloop idea propose` first')
    if cfg.submissions is not None and len(submitted(s, run.id)) >= cfg.submissions:
        raise SubmitRefused(f'run#{run.id} used its {cfg.submissions} submissions')


def submit(s: Session, run: Run, version: Version, repo: Repository, actor: Actor) -> Candidate:
    """Submit ``version`` of ``run``: its candidate, a child of the run's last submission, else of its base.

    Emits ``Submitted``, and keeps the version (``r<run>/s<n>``). The caller commits. A tree that the run submitted
    already is that submission again (``resubmitted``): nothing new, no submission spent, whatever the run's state. A
    tree that another run made, whose verdict a limit gave (``limited``), is this run's submission too, and is measured
    again (``remeasure``).

    Raises:
        SubmitRefused: ``refuse`` refuses it, or the version's tree is a candidate that has its final verdict already.
    """
    write_transaction(s)  # the count of its submissions and the new one in one write: two at once never pass the cap
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    if (own := resubmitted(s, run, version)) is not None:
        remeasure(s, own, actor)
        return own
    refuse(s, run)
    made = submitted(s, run.id)
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    if known is not None and not pending(known) and not remeasure(s, known, actor):
        raise SubmitRefused(f'its tree is candidate#{known.id} already ({label(known)}): change something first')
    parent = s.get_one(Candidate, made[-1]) if made else run.base
    candidate, _ = resolve(s, run.campaign_id, version, parent=parent, run_id=run.id, origin=Origin.RUN)
    repo.keep(version.revision, Pin(run.campaign.name, f'r{run.id}/s{len(made)}'))
    emit(s, Submitted(candidate=candidate.id, revision=version.revision), TargetRef(RUN, run.id), actor)
    return candidate


def waiting(s: Session, campaign: Campaign) -> list[Candidate]:
    """The submitted candidates of ``campaign`` that wait for their verdict, oldest first."""
    runs = select(Run.id).where(Run.campaign_id == campaign.id)
    found = in_order(s, Submitted, Event.target_kind == RUN, Event.target_id.in_(runs))
    candidates = (s.get_one(Candidate, made.candidate) for _, made in found)
    return [c for c in dict.fromkeys(candidates) if pending(c)]
