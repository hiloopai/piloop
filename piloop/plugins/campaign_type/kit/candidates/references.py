"""Candidates that come from elsewhere: references imported from a repo, and how a ref resolves to a version."""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.db.events import emit
from piloop.db.models import Campaign, EventModel
from piloop.ids import CampaignId, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.campaign_type.kit.verdicts.verdict import label
from piloop.plugins.vcs.contract import (
    ReferenceVersion,
    Repository,
    Revision,
    UnknownRevision,
    Version,
)


def references(s: Session, campaign: Campaign) -> list[Candidate]:
    """Return the campaign's imported starting points (``piloop candidate import``), oldest first."""
    query = select(Candidate).where(Candidate.campaign_id == campaign.id, Candidate.origin == Origin.REFERENCE)
    return list(s.scalars(query.order_by(Candidate.id)))


class CandidateImported(EventModel):
    """A reference candidate was imported (``piloop candidate import``)."""

    kind: Literal['candidate.imported'] = 'candidate.imported'
    ref: str
    revision: Revision


def resolve(
    s: Session,
    campaign_id: CampaignId,
    version: Version,
    *,
    parent: Candidate | None,
    run_id: RunId | None,
    origin: Origin,
) -> tuple[Candidate, bool]:
    """Return the candidate for the tree of ``version``: the existing one, or a new row of ``origin`` under ``parent``.

    Returns ``(candidate, created)``. An existing candidate is returned unchanged: a version that reverts to a known
    state is that state, whatever run or version reached it. Two runs that reach one new tree at once share it: the
    insert that loses the race on ``UNIQUE(campaign_id, tree_sha)`` returns the winner's row.
    """
    known = select(Candidate).where(Candidate.campaign_id == campaign_id, Candidate.tree_sha == version.tree)
    found = s.scalar(known)
    if found is not None:
        return found, False
    candidate = Candidate(
        campaign_id=campaign_id,
        run_id=run_id,
        parent_id=parent.id if parent else None,
        tree_sha=version.tree,
        revision=version.revision,
        summary=version.summary,
        origin=origin,
    )
    try:
        with s.begin_nested():
            s.add(candidate)
    except IntegrityError:
        return s.scalars(known).one(), False
    return candidate, True


class ImportRefused(Exception):
    """``import_reference`` cannot register the commit. The message says why."""


def import_reference(
    s: Session,
    campaign: Campaign,
    repo: Repository,
    ref: str,
    *,
    summary: str | None,
    actor: Actor,
) -> Candidate:
    """Register the tree of ``ref`` (a version of the campaign's repo, in its vcs's terms) as a ``reference`` candidate.

    Like the baseline, it has no run and no parent. The version is kept (``ReferenceVersion``), so it outlives what named it.
    The daemon measures it under the current evaluator (``due``).

    Raises:
        ImportRefused: ``ref`` names no version, or its tree is already a candidate of the campaign.
    """
    try:
        version = repo.version(ref)
    except UnknownRevision as e:
        raise ImportRefused(str(e)) from e
    revision = version.revision
    candidate, created = resolve(s, campaign.id, version, parent=None, run_id=None, origin=Origin.REFERENCE)
    if not created:
        raise ImportRefused(f'the tree of {ref!r} already is candidate#{candidate.id} ({label(candidate)})')
    candidate.summary = summary or candidate.summary
    candidate.verdict_reason = f'imported from {ref} ({revision[:7]})'
    repo.keep(revision, ReferenceVersion(campaign.name, candidate.id))
    imported = CandidateImported(ref=ref, revision=revision)
    emit(s, imported, TargetRef(TargetKind.CANDIDATE, candidate.id), actor)
    return candidate
