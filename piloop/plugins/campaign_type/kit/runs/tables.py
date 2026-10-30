"""The table of the ``runs`` component: an agent's unit of work on an idea."""

from datetime import datetime
from typing import TYPE_CHECKING

from covalence import Cid
from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    ForeignKey,
    Index,
    Integer,
    ScalarSelect,
    Select,
    UniqueConstraint,
    and_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from piloop.db.models import NOW, AgentSession, Base, Campaign, CidText, Tracked, by_value, utcnow
from piloop.ids import (
    CampaignId,
    RoleName,
    TargetRef,
)
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.ids import (
    END_STATUS,
    FINISHED_RUNS,
    IMPLEMENTER,
    OPEN_RUNS,
    RUN,
    RunEnd,
    RunId,
    RunStatus,
)

if TYPE_CHECKING:  # the components' tables import this module
    from piloop.plugins.campaign_type.kit.backlog.tables import Idea

# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {
        CandidateId: Integer,
        IdeaId: Integer,
        RunEnd: by_value(RunEnd),
        RunStatus: by_value(RunStatus),
    }
)


def _end_status() -> str:
    """``runs.status`` as a SQL expression of ``runs.end`` (``RunEnd.status``), for the CHECK that keeps them together."""
    cases = ' '.join(f"WHEN '{end}' THEN '{status}'" for end, status in END_STATUS.items())
    return f'CASE "end" {cases} ELSE \'{RunStatus.FAILED}\' END'


class Run(Tracked, Base):
    """One fresh worker session of a role that does runs, with one harness and model, from the head.

    The scheduler starts a run with no idea. Its worker claims one open idea (``piloop idea claim``), or proposes one
    and claims it (``piloop idea propose``). The claim ends with the run.
    """

    __tablename__ = 'runs'
    __table_args__ = (
        UniqueConstraint('idea_id', 'attempt'),
        # what is running or queued: reap, drain and the agents' "what is running" query select by status only
        Index('ix_runs_status', 'status'),
        CheckConstraint(
            f'scoring = 0 OR status IN ({", ".join(repr(s.value) for s in FINISHED_RUNS)})',
            name='runs.scoring needs a finished status',
        ),
        CheckConstraint(
            f'("end" IS NULL) = (status IN ({", ".join(repr(s.value) for s in OPEN_RUNS)}))',
            name='runs.end is set once a run is finished',
        ),
        CheckConstraint(f'"end" IS NULL OR status = {_end_status()}', name='runs.status follows from runs.end'),
    )

    id: Mapped[RunId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(
        ForeignKey('campaigns.id', ondelete='CASCADE', name='fk_runs_campaign_id_campaigns'), index=True
    )
    idea_id: Mapped[IdeaId | None] = mapped_column(ForeignKey('ideas.id', ondelete='CASCADE'))
    """The idea that the run's worker claimed. NULL until it claims one."""
    role: Mapped[RoleName] = mapped_column(default=IMPLEMENTER, server_default=IMPLEMENTER)
    """The role that does the run: ``implementer``, or a role of the campaign with ``runs = true``. Its agents have this
    ``agent_sessions.kind``."""
    attempt: Mapped[int | None]
    """The claimed idea's run count, this one included. NULL until the claim."""
    base_candidate_id: Mapped[CandidateId]
    """The ``candidates.id`` that the worktree started from. The run's candidates descend from it. A plain integer,
    not a foreign key: candidates reference runs, and SQLite cannot add a foreign key for a cycle afterwards."""
    branch: Mapped[bool] = mapped_column(default=False, server_default=text('0'))
    """Whether the run started from an alternative starting point, not the head: it is judged against its own base
    only, so what it keeps is a branch keep, which never moves the head. The scheduler starts every run from the head;
    a run of 0.5 that an idea pinned elsewhere is one."""
    harness: Mapped[str]
    model: Mapped[str]
    status: Mapped[RunStatus] = mapped_column(default=RunStatus.QUEUED, server_default=RunStatus.QUEUED.value)
    scoring: Mapped[bool] = mapped_column(default=False, server_default=text('0'))
    """Whether the daemon scores the run's commits now. It is set when the implementer ends and the run gets its final
    ``status``, and cleared when the commits are judged and the idea moved on, whatever the idea's status. While it is
    set, the run keeps its place under ``[stop] max_runs``, and a restarted daemon resumes the scoring."""
    brief_cid: Mapped[Cid | None] = mapped_column(CidText)
    """The exact prompt that the implementer got, a text blob in the CAS. NULL until the run starts."""
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    end: Mapped[RunEnd | None]
    """How the run ended. NULL while it is queued or running. ``status`` follows from it."""
    error: Mapped[str | None]
    """The detail of the end, for a human: the process error, the escalation that waits, or why it failed."""
    log_dir: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    campaign: Mapped[Campaign] = relationship()
    idea: Mapped[Idea | None] = relationship(back_populates='runs')
    base: Mapped[Candidate] = relationship(primaryjoin='foreign(Run.base_candidate_id) == Candidate.id', viewonly=True)
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates='run', foreign_keys='Candidate.run_id', order_by='Candidate.id'
    )


def campaign_runs(campaign_id: CampaignId) -> Select[Run]:
    """Every run of the campaign."""
    return select(Run).where(Run.campaign_id == campaign_id)


def latest_agent_session(s: Session, run_id: RunId) -> AgentSession | None:
    """The latest agent row that did run ``run_id``. A relaunch after the agent failed to start is a second row."""
    return s.scalars(select(AgentSession).where(working_on(run_id)).order_by(AgentSession.id.desc()).limit(1)).first()


def run_of(agent_session: AgentSession) -> RunId | None:
    """The run that ``agent_session`` works on (its subject ``run#N``), or None: a sweep, or other work."""
    match agent_session.subject:
        case TargetRef(kind=kind, id=id) if kind == RUN:
            return RunId(id)
        case _:
            return None


def working_on(run: RunId | ColumnElement[int] | ScalarSelect[int | None]) -> ColumnElement[bool]:
    """Whether an agent session works on ``run``: its subject is ``run#<run>``."""
    return and_(AgentSession.subject_kind == RUN, AgentSession.subject_id == run)
