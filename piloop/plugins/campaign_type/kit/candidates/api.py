"""The candidates' route: a reference imported from the campaign's repo."""

from contextlib import closing
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends

from piloop.api.contract import (
    Problem,
)
from piloop.campaign.campaigns import (
    repository,
)
from piloop.daemon.api import REFUSALS_DOCUMENTED, Acting, Addressed, Writing, actor_access, reader
from piloop.db import feed as db_feed
from piloop.net import Refusal
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.references import import_reference
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import CandidateAnswer, ImportBody
from piloop.plugins.campaign_type.kit.verdicts.verdict import candidate_view

router = APIRouter(prefix='/v1', dependencies=[Depends(reader)], responses=REFUSALS_DOCUMENTED)
"""The candidates' route, beside the API's."""


def candidate_addressed(s: Writing, candidate: CandidateId) -> Candidate:
    """The candidate that the path names. Refuses one that the home does not have."""
    if (found := s.get(Candidate, candidate)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CANDIDATE, f'no candidate#{candidate}')
    return found


AddressedCandidate = Annotated[Candidate, Depends(candidate_addressed)]


@router.post('/campaigns/{campaign}/candidates', operation_id='import_candidate', dependencies=[Depends(actor_access)])
def import_candidate(s: Writing, me: Acting, campaign: Addressed, body: ImportBody) -> CandidateAnswer:
    """Register the files of a version as a reference candidate: a starting point for an idea."""
    with closing(repository(campaign)) as repo:
        candidate = import_reference(s, campaign, repo, body.version, summary=body.summary, actor=me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return CandidateAnswer(cursor=cursor, candidate=candidate_view(candidate))
