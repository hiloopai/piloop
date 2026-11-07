from piloop.api.views import Rendered, Table, Value
from piloop.db.models import count
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate, produced
from piloop.plugins.campaign_type.kit.evaluation.ids import Measured, TrackName
from piloop.plugins.campaign_type.kit.evaluation.measure import status
from piloop.plugins.campaign_type.kit.pool.pool import UNSCORED, Pool, Standing, track_of
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.text import age, cell

CANDIDATES_SHOWN = 8
PLACES = {
    Measured.UNMEASURED: 'not scored',
    Measured.CLIMBING: 'not scored',
    Measured.STOPPED: 'stopped',
    Measured.CRASHED: 'crash',
    Measured.CANCELLED: 'cancelled',
    Measured.SCORED: 'not scored',
}
"""The place of a candidate without a mean of the primary, by its ``Status.state``."""


def place(standing: Standing) -> str:
    """Where a candidate stands, in a cell: ``best``, ``#2 of 9``, ``2/3 evals``, ``refused``, or ``crash``."""
    if standing.mean is None:
        return PLACES[standing.status.state]
    if standing.best == standing.candidate:
        return 'best'
    if standing.excluded is not None:
        return 'excluded'
    if standing.refused is not None:
        return 'refused'
    if standing.rank is not None:
        return f'#{standing.rank} of {standing.of}'
    return f'{standing.status.scored} eval' + ('s' if standing.status.scored != 1 else '')


class Candidates(Section):
    """The latest candidates, oldest first, each with where it stands and its primary score against its parent's.

    With a ladder, each also says where its latest climb is (``climb``).
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 180

    def render(self, r: Rendering) -> Rendered:
        s, campaign = r.s, r.campaign
        cfg = campaign.config
        evaluator_id = campaign.evaluator_id
        total = count(s, produced(campaign))
        recent = s.scalars(produced(campaign).order_by(Candidate.id.desc()).limit(CANDIDATES_SHOWN)).all()[::-1]
        on = "each on its track's primary" if cfg.multitrack else cfg.all_tracks[0].primary.name
        pools: dict[TrackName, Pool] = {}
        rows: list[tuple[Value, ...]] = []
        for c in recent:
            track = track_of(cfg, c)
            pool = pools.get(track.name) or pools.setdefault(track.name, Pool.of(s, campaign, track))
            standing = None if c.origin in UNSCORED else pool.standing(c.id)
            rows.append(
                (
                    f'candidate#{c.id}',
                    c.origin if standing is None else place(standing),
                    *([track.name] if cfg.multitrack else []),
                    None if standing is None else standing.mean,
                    None if standing is None else standing.vs_parent,
                    *([status(c, evaluator_id).climb()] if cfg.ladder else []),
                    f'run#{c.run_id}' if c.run_id else 'no run',
                    age(c.created_at, r.now),
                    cell(c.summary or ''),
                )
            )
        columns = (
            'candidate',
            'standing',
            *(['track'] if cfg.multitrack else []),
            'score',
            'vs parent',
            *(['ladder'] if cfg.ladder else []),
            'run',
            'age',
        )
        table = Table(columns=(*columns, 'summary'), rows=tuple(rows))
        return Rendered(title=f'Recent candidates ({len(recent)} of {total}; {on})', blocks=(table,))
