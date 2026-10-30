"""Submissions: the versions that an implementer sends to be scored while it works (``piloop submit``).

A submission is a ``run.submitted`` event on its run. The daemon measures and judges each one at once, against the
head (``RunController.judge_submission``), and the implementer waits for its verdict and score. A run may submit
``[evaluator] submissions`` versions.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.ops import Refused
from piloop.db.events import emit, in_order
from piloop.db.models import AgentSession, Campaign, Event, EventModel
from piloop.db.session import write_transaction
from piloop.ids import Revision, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.references import resolve
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, run_of
from piloop.plugins.campaign_type.kit.verdicts.verdict import label, pending
from piloop.plugins.vcs.contract import Pin, Repository, Version


class Submitted(EventModel):
    """An implementer submitted a version of its run: the daemon measures and judges its candidate now."""

    kind: Literal['run.submitted'] = 'run.submitted'
    candidate: CandidateId
    revision: Revision


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


def refuse(s: Session, run: Run) -> None:
    """Refuse a new submission of ``run`` before anything of it is taken in.

    Raises:
        SubmitRefused: the campaign scores its runs after they end, the run is not running or has claimed no idea, or
            it used its submissions.
    """
    campaign = run.campaign
    cfg = campaign.config.evaluator
    if cfg.scoring is not Scoring.SUBMIT:
        raise SubmitRefused(f'campaign {campaign.name} scores every commit after its run ends: commit, and end the run')
    if run.status is not RunStatus.RUNNING:
        raise SubmitRefused(f'run#{run.id} is {run.status}, not running')
    if run.idea_id is None:  # judged on no track, its version could move the head for no idea
        raise SubmitRefused(f'run#{run.id} has claimed no idea: `piloop idea claim` or `piloop idea propose` first')
    if len(submitted(s, run.id)) >= cfg.submissions:
        raise SubmitRefused(f'run#{run.id} used its {cfg.submissions} submissions')


def submit(s: Session, run: Run, version: Version, repo: Repository, actor: Actor) -> Candidate:
    """Submit ``version`` of ``run``: its candidate, a child of the run's last submission, else of its base.

    Emits ``Submitted``, and keeps the version (``r<run>/s<n>``). The caller commits. A tree that the run submitted
    already is that submission again (``resubmitted``): nothing new, no submission spent, whatever the run's state.

    Raises:
        SubmitRefused: ``refuse`` refuses it, or the version's tree is a candidate that has its verdict already.
    """
    write_transaction(s)  # the count of its submissions and the new one in one write: two at once never pass the cap
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    if (own := resubmitted(s, run, version)) is not None:
        return own
    refuse(s, run)
    made = submitted(s, run.id)
    known = s.scalar(
        select(Candidate).where(Candidate.campaign_id == run.campaign_id, Candidate.tree_sha == version.tree)
    )
    if known is not None and not pending(known):
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
