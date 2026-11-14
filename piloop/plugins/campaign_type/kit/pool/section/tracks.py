from collections import Counter

from sqlalchemy import select

from piloop.api.views import Lines, Rendered
from piloop.plugins.campaign_type.kit.backlog.ideas import claimable
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.plugins.campaign_type.kit.runs.ids import OPEN_RUNS
from piloop.plugins.campaign_type.kit.runs.tables import Run, RunClaim
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.overview import num
from piloop.text import age, metric


class Tracks(Section):
    """One line per track. Nothing with one track.

    A line shows the track's primary at its best against the baseline and its floor, how often a candidate became its
    best, its open runs, and its open ideas.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 120

    def render(self, r: Rendering) -> Rendered | None:
        s, campaign = r.s, r.campaign
        cfg = campaign.config
        if not cfg.multitrack:
            return None
        open_runs = (
            select(Idea.track)
            .join(RunClaim, RunClaim.idea_id == Idea.id)
            .join(Run, Run.id == RunClaim.run_id)
            .where(Run.campaign_id == campaign.id, Run.status.in_(OPEN_RUNS))
        )
        running = Counter(s.scalars(open_runs))
        tracks = select(Idea.track).where(Idea.campaign_id == campaign.id)
        ready = Counter(s.scalars(tracks.where(claimable())))
        lines = []
        for track in cfg.all_tracks:
            m, pool = track.primary, Pool.of(s, campaign, track)
            best = pool.best()
            value = pool.mean(best)
            gain = pool.gain(best, pool.baseline) if best is not None else None
            on = f' ({metric(gain, signed=True)} on the baseline)' if gain is not None else ''
            floor = ''
            if m.floor is not None:
                floor = f'; floor {metric(m.floor)}' + (
                    f', {metric(m.gain(value, m.floor))} to spare' if value is not None else ''
                )
            became = new_bests(s, campaign, track.name)
            latest = f', the latest {age(became[-1][0].at, r.now)} ago' if became else ''
            at = f'{m.name} {num(value)} at candidate#{best}{on}' if best is not None else 'no baseline yet'
            lines.append(
                f'- {track.name}: {at}{floor}; {len(became)} new bests{latest};'
                f' {running[track.name]} running;'
                f' {ready[track.name]} open ideas.'
            )
        return Rendered(title='Tracks', blocks=(Lines(lines=tuple(lines)),))
