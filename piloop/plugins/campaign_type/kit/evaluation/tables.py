"""The tables of the ``evaluation`` component: the evaluators, the evaluation queue and the measurements."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from piloop.db.models import EMPTY_JSON, NOW, PROCESS, Base, Campaign, text_checks, utcnow
from piloop.ids import CampaignId
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, MeasurementId, Reading, RungName

# the types of its columns, beside core's in Base.type_annotation_map
Base.registry.update_type_annotation_map(
    {
        CandidateId: Integer,
        MeasurementId: Integer,
        RungName: String,
    }
)


class Evaluator(Base):
    """One version of how a campaign is scored: the evaluate command, the judging metrics and the linked inputs.

    ``piloop init`` copies it from ``piloop.toml``, and a change makes a new row. A measurement points at the
    evaluator that produced it, so a number always says what it means, and an old number is never taken for a new one.
    """

    __tablename__ = 'evaluators'
    __table_args__ = (UniqueConstraint('campaign_id', 'key'),)

    id: Mapped[EvaluatorId] = mapped_column(primary_key=True)
    campaign_id: Mapped[CampaignId] = mapped_column(ForeignKey('campaigns.id', ondelete='CASCADE'))
    key: Mapped[str]
    """A short hash of ``definition``, from ``CampaignConfig.evaluator_key``."""
    definition: Mapped[dict[str, object]]
    """``evaluate``, ``link``, and ``judging``: the name and direction of each primary and guardrail. A watch metric is
    not part of it, so adding one measures nothing again. An older row that no current config names has ``metrics``,
    every metric, instead."""
    runner: Mapped[str] = mapped_column(default=PROCESS, server_default=PROCESS)
    """The evaluator runner, by name: how piloop runs the evaluator to measure a candidate. ``process``, built in, runs
    the evaluate command as a process in a clean worktree of the candidate, and is the runner of every evaluator for
    now. ``piloop.plugins.campaign_type.kit.evaluation.measure.current_evaluator`` refuses a name that no installed runner has. It is not part of
    ``key`` yet: with one runner, every evaluator has it."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)


def evaluator_of(s: Session, campaign: Campaign) -> Evaluator | None:
    """The campaign's current evaluator (``campaigns.evaluator_id``), or None before its first."""
    return None if campaign.evaluator_id is None else s.get(Evaluator, campaign.evaluator_id)


class Measurement(Base):
    """One run of an evaluator on a candidate. A candidate can have many: repeats, or a new version of the evaluator."""

    __tablename__ = 'measurements'
    __table_args__ = (
        Index('ix_measurements_candidate_evaluator', 'candidate_id', 'evaluator_id'),
        CheckConstraint(
            'stopped IS NULL OR (rung IS NOT NULL AND error IS NULL)',
            name='measurements.a stop is at a rung, with no error',
        ),
        *text_checks('measurements', nullable=('rung', 'stopped')),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[MeasurementId] = mapped_column(primary_key=True)
    candidate_id: Mapped[CandidateId] = mapped_column(ForeignKey('candidates.id', ondelete='CASCADE'))
    evaluator_id: Mapped[EvaluatorId] = mapped_column(ForeignKey('evaluators.id'))
    metrics: Mapped[dict[str, float]] = mapped_column(default=dict, server_default=EMPTY_JSON)
    error: Mapped[str | None]
    """Set when the evaluate command failed. ``metrics`` then has what it printed before it failed."""
    seconds: Mapped[float | None]
    rung: Mapped[RungName | None]
    """The rung of the evaluation ladder that it measured (``[[ladder]]``). NULL for the full panel: only a measurement
    of the full panel scores."""
    stopped: Mapped[str | None]
    """Why the climb stopped at ``rung``: the stop that held. NULL when the climb went on."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    candidate: Mapped[Candidate] = relationship(back_populates='measurements')
    evaluator: Mapped[Evaluator] = relationship()

    @property
    def reading(self) -> Reading:
        """What it says of its candidate: the one reading of ``error``, ``rung`` and ``stopped``."""
        return read(self.error, self.rung, self.stopped)


def read(error: str | None, rung: RungName | None, stopped: str | None) -> Reading:
    """What a measurement with these columns says of its candidate, for a reader of columns, not rows (``Pool.of``)."""
    if error is not None:
        return Reading.CRASH
    if rung is None:
        return Reading.SCORE
    return Reading.CLIMB if stopped is None else Reading.STOP


class Cancel(Base):
    """Someone cancelled the evaluation of a candidate under one evaluator: the daemon does not evaluate it there unasked.

    ``piloop eval cancel`` writes it (``queue.cancel``), and an ask to measure the candidate again
    (``request_remeasure``) deletes it. A cancel never changes where a candidate came from (``Candidate.origin``).
    """

    __tablename__ = 'cancels'
    __table_args__ = (*text_checks('cancels', 'cancelled_by'),)

    candidate_id: Mapped[CandidateId] = mapped_column(ForeignKey('candidates.id', ondelete='CASCADE'), primary_key=True)
    evaluator_id: Mapped[EvaluatorId] = mapped_column(ForeignKey('evaluators.id'), primary_key=True)
    cancelled_by: Mapped[str]
    cancelled_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)

    candidate: Mapped[Candidate] = relationship(back_populates='cancels')


class Evaluation(Base):
    """An evaluation that the daemon needs: it waits for a slot of the executor or runs. The evaluation queue.

    The daemon inserts a row when it needs a measurement, and deletes it when the measurement is recorded, so the table
    holds only what waits or runs. A free slot goes to the campaign with the fewest running rows, and in it to its best
    first, then to the lowest id (``piloop.plugins.campaign_type.kit.evaluation.queue``). The requests for one candidate under one evaluator share
    one row: a tree is evaluated once. An id is never reused, so a request that waits for a row never mistakes a later
    one for it. A daemon that starts deletes the rows that the previous one left, and asks again for what it still
    needs. ``piloop eval queue`` lists the rows, and ``piloop eval cancel`` cancels one.
    """

    __tablename__ = 'evaluations'
    __table_args__ = (
        UniqueConstraint('candidate_id', 'evaluator_id'),
        CheckConstraint(
            'cancelled_by IS NULL OR started_at IS NOT NULL', name='evaluations.only a running evaluation is cancelled'
        ),
        *text_checks('evaluations', nullable=('cancelled_by',)),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[CandidateId] = mapped_column(ForeignKey('candidates.id', ondelete='CASCADE'))
    evaluator_id: Mapped[EvaluatorId] = mapped_column(ForeignKey('evaluators.id'))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
    started_at: Mapped[datetime | None]
    """When it got a slot. NULL while it waits."""
    cancelled_by: Mapped[str | None]
    """Who cancelled it while it ran: the daemon stops the evaluator and records nothing. A waiting one that is
    cancelled leaves the queue instead, so only a running one has it (a CHECK)."""
    ask: Mapped[int | None]
    """The remeasure that it answers (``RemeasureRequested``), by the id of its event: set by the request that made it
    for the ask. NULL for any other. Only its own evaluation answers an ask: one that ran before it, or that another
    request made, measures the candidate but leaves the ask open."""

    candidate: Mapped[Candidate] = relationship()
