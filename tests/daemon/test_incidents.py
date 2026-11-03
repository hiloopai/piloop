"""The incident triggers (a failed run, an agent dead at startup) find what wakes an agent; the cost sections show what the progress review weighs."""

from pathlib import Path

from inline_snapshot import snapshot
from sqlalchemy.orm import Session

from piloop.db.events import AnomalyReported
from piloop.db.models import Campaign, Event
from piloop.db.session import Database
from piloop.ids import TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunEnd, RunStatus
from piloop.plugins.role.orchestrator.passes import Fire, RolePass
from piloop.plugins.role.orchestrator.run_failed import run_failed
from piloop.plugins.section.contract import SECTIONS, Rendering, markdown
from piloop.plugins.section.reported import REPORTED_SHOWN
from tests.conftest import LEAD, cas_of
from tests.reports.test_overview import NOW, agent, candidate, idea, live_campaign, run


def fired(s: Session, campaign: Campaign) -> Fire | None:
    """What ``run_failed`` asks for the orchestrator on a pass now."""
    return run_failed(
        RolePass(s=s, now=NOW, campaign=s.get_one(Campaign, campaign.id), role=LEAD, last=None, quiet=None)
    )


def subjects(fire: Fire | None) -> list[str]:
    return [] if fire is None else [str(e.subject) for e in fire.events if isinstance(e, AnomalyReported)]


def shown(s: Session, campaign: Campaign, *names: str) -> str:
    sections = [SECTIONS.validated({'provider': name}) for name in names]
    return markdown(sections, Rendering(s, NOW, s.get_one(Campaign, campaign.id), Path('/no/workspace'), cas_of(s)))


def test_a_failed_or_timed_out_run_is_an_incident_and_a_stopped_or_given_up_one_is_not(
    db: Database, campaign: Campaign
) -> None:
    """A run that the daemon stopped did nothing wrong, and a worker that gave up its run said why."""
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        for status, end in ((RunStatus.TIMED_OUT, None), (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)):
            row = run(s, idea(s, p, f'{status}', IdeaStatus.DONE), status, 1, 60, 70)
            row.end = end or row.end
        given_up = run(s, idea(s, p, 'given up', IdeaStatus.OPEN), RunStatus.FAILED, 1, 60, 70, error='too slow')
        given_up.end = RunEnd.GAVE_UP
        s.commit()
        fire = fired(s, campaign)
    assert fire is not None
    assert subjects(fire) == ['run#2', 'run#4'], 'oldest first'
    assert fire.reason == 'run_failed: run#2 failed, run#4 failed'
    [first, _] = fire.events
    assert isinstance(first, AnomalyReported) and first.line == snapshot(
        '- run#2 failed, 1h ago: failed on script (idea#2): boom'
    )


def test_an_incident_wakes_an_agent_once(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    with db.session() as s:
        fire = fired(s, campaign)
        assert fire is not None
        for event in fire.events:
            s.add(Event(actor='daemon', target=TargetRef(TargetKind.AGENT_SESSION, 1), payload=event))
        s.commit()
        assert fired(s, campaign) is None, 'an agent of the campaign got it'


def test_the_incidents_given_to_agents_are_listed_newest_first(db: Database, campaign: Campaign) -> None:
    with db.session() as s:
        assert shown(s, campaign, 'reported') == '## Incidents given to agents\n\n- None.'
        sweeper = agent(s, s.get_one(Campaign, campaign.id), LEAD, 0, 5)
        for run_id in range(3, 3 + REPORTED_SHOWN + 1):
            subject = TargetRef(RUN, run_id)
            reported = AnomalyReported(subject=subject, trigger='run_failed', line=f'- {subject}')
            s.add(
                Event(actor='daemon', target=TargetRef(TargetKind.AGENT_SESSION, sweeper.id), payload=reported, at=NOW)
            )
        s.commit()
        text = shown(s, campaign, 'reported')
    lines = text.splitlines()[2:]
    assert lines[:2] == [
        f'- run#{2 + REPORTED_SHOWN + 1} (run_failed), given to agent_session#1 0s ago',
        f'- run#{2 + REPORTED_SHOWN} (run_failed), given to agent_session#1 0s ago',
    ], 'newest first'
    assert (len(lines), lines[-1]) == (REPORTED_SHOWN + 1, '- … and 1 older incidents.')


def test_candidates_that_scored_exactly_their_parent_share_a_line(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        for n in range(9):
            candidate(s, p, 2, Outcome.DISCARD, 3, 30 + n, run_id=1)
        orphan = Candidate(campaign_id=p.id, parent_id=1, tree_sha='x', revision='x', verdict=Outcome.CRASH)
        s.add(orphan)
        s.flush()
        s.add(Measurement(candidate_id=orphan.id, evaluator_id=p.evaluator_id, metrics={}, error='died'))
        s.commit()
        text = shown(s, campaign, 'same_as_parent')
    assert text.splitlines()[2] == snapshot(
        '- 10 candidates (candidate#12, candidate#11, candidate#10, candidate#9, candidate#8, candidate#7, candidate#6, candidate#5, …); the latest, 1h ago: scored what candidate#2 did on score and on 1 of 1 other metrics; 3.0 min of evaluation'
    ), 'a crashed measurement is no score'


def test_one_candidate_that_scored_exactly_its_parent_has_a_line_of_its_own(db: Database, campaign: Campaign) -> None:
    live_campaign(db, campaign)
    with db.session() as s:
        assert shown(s, campaign, 'same_as_parent').splitlines()[2].startswith('- candidate#3, 1h ago: scored what')
        s.get_one(Measurement, 3).metrics = {'score': 4.0, 'size': 10.0}
        s.commit()
        assert shown(s, campaign, 'same_as_parent') == '', 'nothing to show'
