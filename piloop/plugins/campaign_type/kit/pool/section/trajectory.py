from piloop.api.views import Lines, Rendered
from piloop.db.models import count
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.scored import new_bests
from piloop.plugins.campaign_type.kit.runs.ids import FINISHED_RUNS
from piloop.plugins.campaign_type.kit.runs.stop import attempt
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.overview import num
from piloop.text import age, cell, metric

STEPS_SHOWN = 8
"""How many of the latest new bests, and of the other lines, it lists."""


class Trajectory(Section):
    """The best of the first track: the baseline, the best now, the gain between them, and the latest new bests.

    Then come the other lines worth branching from (``Pool.worth_branching``).
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 130

    def render(self, r: Rendering) -> Rendered:
        s, campaign = r.s, r.campaign
        track = campaign.config.all_tracks[0]
        pool, m = Pool.of(s, campaign, track), track.primary
        best = pool.best()
        if best is None or pool.baseline is None:
            return Rendered(title='Trajectory', blocks=(Lines(lines=('- No baseline yet.',)),))
        a, b = pool.mean(pool.baseline), pool.mean(best)
        if a is None or b is None:
            gain = f'{m.name} unmeasured'
        else:
            relative = f', {(b - a) / abs(a):+.1%}' if a else ''
            gain = f'{m.name} {metric(a)} → {metric(b)} ({metric(b - a, signed=True)}{relative})'
        became = new_bests(s, campaign, track.name)
        lines = [f'- Baseline candidate#{pool.baseline}, best candidate#{best}: {gain}; {len(became)} new bests.']
        if became:
            finished = campaign_runs(campaign.id).where(
                Run.status.in_(FINISHED_RUNS), attempt(), Run.finished_at > became[-1][0].at
            )
            lines += [
                f'- Last new best {age(became[-1][0].at, r.now)} ago; {count(s, finished)} runs finished since.',
                f'- New bests (latest {STEPS_SHOWN}; the gain over the one before):',
            ]
            scores = [a, *(scored.score for _, scored in became)]
            for i, (event, scored) in enumerate(became):
                if i < len(became) - STEPS_SHOWN:
                    continue
                previous, value = scores[i], scored.score
                delta = (
                    '' if previous is None or value is None else f' ({metric(m.gain(value, previous), signed=True)})'
                )
                c = s.get_one(Candidate, event.target_id)
                idea = f' idea#{c.run.idea_id}' if c.run is not None else ''
                lines.append(
                    f'  - {event.at:%m-%d %H:%M} candidate#{c.id} {num(value)}{delta}{idea}: {cell(c.summary or "—", 80)}'
                )
        others = pool.worth_branching(STEPS_SHOWN)[1:]
        if others:
            lines.append('- Other lines (their best, against the best):')
            for c in others:
                gain_on = pool.gain(c, best)
                versus = f' ({metric(gain_on, signed=True)})' if gain_on is not None else ''
                summary = s.get_one(Candidate, c).summary or '—'
                lines.append(f'  - candidate#{c} {num(pool.mean(c))}{versus}: {cell(summary, 80)}')
        return Rendered(title='Trajectory', blocks=(Lines(lines=tuple(lines)),))
