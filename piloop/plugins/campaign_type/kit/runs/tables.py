"""The table of the ``runs`` component: an agent's unit of work on an idea."""

from datetime import datetime

from covalence import Cid
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Select,
    UniqueConstraint,
    select,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from piloop.db.models import NOW, Base, CidText, Tracked, by_value, utcnow
from piloop.ids import CampaignId, RoleName
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.ids import (
    END_STATUS,
    FINISHED_RUNS,
    IMPLEMENTER,
    OPEN_RUNS,
    RunEnd,
    RunId,
    RunStatus,
)

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
    """One attempt at an idea, by one agent of a role that does runs, with one harness and model."""

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
    idea_id: Mapped[IdeaId] = mapped_column(ForeignKey('ideas.id', ondelete='CASCADE'))
    role: Mapped[RoleName] = mapped_column(default=IMPLEMENTER, server_default=IMPLEMENTER)
    """The role that does the run: ``implementer``, or a role of the campaign with ``runs = true``. Its agents have this
    ``agent_sessions.kind``."""
    attempt: Mapped[int]
    base_candidate_id: Mapped[CandidateId]
    """The ``candidates.id`` that the worktree started from. The run's candidates descend from it. A plain integer,
    not a foreign key: candidates reference runs, and SQLite cannot add a foreign key for a cycle afterwards."""
    branch: Mapped[bool] = mapped_column(default=False, server_default=text('0'))
    """Whether the run started from an alternative starting point: its idea pinned a base other than the head when it
    was queued (a reference, an older keep or a note). The run is judged against its own base only, so what it keeps
    is a branch keep, which never moves the head. The scheduler sets it (``explore_every`` of the run's role)."""
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

    idea: Mapped[Idea] = relationship(back_populates='runs')
    base: Mapped[Candidate] = relationship(primaryjoin='foreign(Run.base_candidate_id) == Candidate.id', viewonly=True)
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates='run', foreign_keys='Candidate.run_id', order_by='Candidate.id'
    )


def campaign_runs(campaign_id: CampaignId) -> Select[Run]:
    """Every run of the campaign. A run has no ``campaign_id``, but its idea has one."""
    return select(Run).join(Idea).where(Idea.campaign_id == campaign_id)
