"""The ``same_as_parent`` section: the candidates that scored exactly what their parent did."""

from pathlib import Path

import pytest
from inline_snapshot import snapshot
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.section.contract import SECTIONS, Rendering, markdown
from tests.conftest import cas_of
from tests.reports.test_overview import NOW, candidate, live_campaign

pytestmark = pytest.mark.usefixtures('auditor')


def shown(s: Session, campaign: Campaign) -> str:
    section = SECTIONS.validated({'provider': 'same_as_parent'})
    return markdown([section], Rendering(s, NOW, s.get_one(Campaign, campaign.id), Path('/no/workspace'), cas_of(s)))


def test_candidates_that_scored_exactly_their_parent_share_a_line(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        for n in range(9):
            candidate(s, p, 2, 3, 30 + n, run_id=1)
        orphan = Candidate(campaign_id=p.id, parent_id=1, tree_sha='x', revision='x')
        s.add(orphan)
        s.flush()
        s.add(Measurement(candidate_id=orphan.id, evaluator_id=p.evaluator_id, metrics={}, error='died'))
        s.commit()
        text = shown(s, campaign)
    assert text.splitlines()[2] == snapshot(
        '- 10 candidates (candidate#12, candidate#11, candidate#10, candidate#9, candidate#8, candidate#7, candidate#6, candidate#5, …); the latest, 1h ago: scored what candidate#2 did on score and on 1 of 1 other metrics; 3.0 min of evaluation'
    ), 'a crashed measurement is no score'


def test_one_candidate_that_scored_exactly_its_parent_has_a_line_of_its_own(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    with db.session() as s:
        assert shown(s, campaign).splitlines()[2].startswith('- candidate#3, 1h ago: scored what')
        s.get_one(Measurement, 3).metrics = {'score': 4.0, 'size': 10.0}
        s.commit()
        assert shown(s, campaign) == '', 'nothing to show'
