"""The table of the ``pool`` component: the best rule's answers."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from piloop.db.models import NOW, Base, by_value, text_checks, utcnow
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId


class Answer(StrEnum):
    """What the best rule answered to one case (``Judgement.answer``)."""

    KEEP = 'keep'
    """The candidate replaced the best."""
    REFUSE = 'refuse'
    """It may not be best: ``reason`` says why."""
    UNANSWERED = 'unanswered'
    """The rule could not decide: ``reason`` holds its error. The best waits at the candidate until a new case or a
    changed ``[plugins.best_rule]`` table asks again."""


Base.registry.update_type_annotation_map({Answer: by_value(Answer)})


class Judgement(Base):
    """The best rule's answer to one case: may ``candidate`` replace ``incumbent`` as the best of its track.

    ``key`` names the case: a hash of the ``[plugins.best_rule]`` table, the track's metrics, and the measurements of
    each side. The daemon asks the rule once for each key, and the best of each track is a fold over the stored answers
    (``piloop.plugins.campaign_type.kit.pool.pool.Pool.fold``).
    """

    __tablename__ = 'judgements'
    __table_args__ = (
        UniqueConstraint('candidate_id', 'key'),
        *text_checks('judgements', 'key', nullable=('reason',)),
        {'sqlite_autoincrement': True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[CandidateId] = mapped_column(ForeignKey('candidates.id', ondelete='CASCADE'))
    incumbent_id: Mapped[CandidateId] = mapped_column(ForeignKey('candidates.id', ondelete='CASCADE'))
    """The best that the candidate would replace."""
    evaluator_id: Mapped[EvaluatorId] = mapped_column(ForeignKey('evaluators.id'))
    """The evaluator whose pool asked."""
    key: Mapped[str]
    answer: Mapped[Answer]
    reason: Mapped[str | None]
    """Why, as the rule said: what the worker hears. Never name a hidden unit in it."""
    seconds: Mapped[float]
    """How long the rule took."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=NOW)
