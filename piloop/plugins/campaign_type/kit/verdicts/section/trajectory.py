from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.api.views import Lines, Rendered
from piloop.config import CampaignConfig
from piloop.db.models import Campaign, count
from piloop.plugins.campaign_type.kit.candidates.ids import Cause
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import known, score
from piloop.plugins.campaign_type.kit.runs.ids import FINISHED_RUNS
from piloop.plugins.campaign_type.kit.runs.stop import attempt
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.campaign_type.kit.verdicts.judge import baseline, track_of, trunk_keeps
from piloop.plugins.campaign_type.kit.verdicts.verdict import noted_by
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.overview import num
from piloop.text import age, cell, metric

STEPS_SHOWN = 8
"""How many of the latest trunk keeps, and of the latest notes, it lists."""


class Trajectory(Section):
    """The trunk: the start, the head, the gain of each track between them, and the latest trunk keeps.

    Then come the latest notes: leads that a run can take a change from.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.SWEEP})
    ORDER = 130

    def render(self, r: Rendering) -> Rendered:
        s, campaign = r.s, r.campaign
        cfg = campaign.config
        evaluator_id = campaign.evaluator_id
        steps = trunk_keeps(s, campaign)
        if not steps:
            line = "- No trunk keep: the baseline's verdict was overridden and nothing was kept since."
            lines = [line if baseline(s, campaign) is not None else '- No baseline yet.']
            return Rendered(title='Trajectory', blocks=(Lines(lines=tuple(lines)),))
        first, last = steps[0], steps[-1]
        gains = []
        for track in cfg.all_tracks:
            a, b = score(first, evaluator_id, track.primary), score(last, evaluator_id, track.primary)
            if a is None or b is None:
                gains.append(f'{track.primary.name} unmeasured')
                continue
            relative = f', {(b - a) / abs(a):+.1%}' if a else ''
            gains.append(f'{track.primary.name} {metric(a)} → {metric(b)} ({metric(b - a, signed=True)}{relative})')
        finished = campaign_runs(campaign.id).where(
            Run.status.in_(FINISHED_RUNS), attempt(), Run.finished_at > known(last)
        )
        lines = [
            f'- Start candidate#{first.id}, head candidate#{last.id}: {"; ".join(gains)}; {len(steps) - 1} trunk keeps.',
            f'- Last trunk keep {age(known(last), r.now)} ago; {count(s, finished)} runs finished since.',
            f'- Trunk keeps (latest {STEPS_SHOWN}; when first measured, the gain on its track over the head before it):',
        ]
        before = dict(zip([c.id for c in steps[1:]], steps, strict=False))
        for c in steps[-STEPS_SHOWN:]:
            track = track_of(cfg, c)
            value, previous = score(c, evaluator_id, track.primary), before.get(c.id)
            prior = score(previous, evaluator_id, track.primary) if previous is not None else None
            if previous is None:
                delta = ' (start)'
            else:
                delta = f' ({value - prior:+g})' if value is not None and prior is not None else ''
            on = f' [{track.name}] {track.primary.name}' if cfg.multitrack and previous is not None else ''
            idea = f' idea#{c.run.idea_id}' if c.run is not None else ''
            lines.append(
                f'  - {known(c):%m-%d %H:%M} candidate#{c.id}{on} {num(value)}{delta}{idea}:'
                f' {cell(c.summary or "—", 80)}'
            )
        lines += _notes(s, campaign, cfg)
        return Rendered(title='Trajectory', blocks=(Lines(lines=tuple(lines)),))


def _notes(s: Session, campaign: Campaign, cfg: CampaignConfig) -> list[str]:
    """List the latest notes."""
    evaluator_id = campaign.evaluator_id
    lines = []
    noted = s.scalars(
        select(Candidate)
        .where(Candidate.campaign_id == campaign.id, Candidate.cause == Cause.NOTE)
        .order_by(Candidate.id.desc())
        .limit(STEPS_SHOWN)
    ).all()
    if noted:
        lines.append('- Notes (regressed, kept in view as leads; a worker takes the change of one):')
        lines += [
            f'  - candidate#{c.id} {num(score(c, evaluator_id, track_of(cfg, c).primary))} by {noted_by(s, c)}:'
            f' {cell(c.verdict_reason or "", 100)}'
            for c in noted
        ]
    return lines
