"""``latency``: a table of the latest candidates' latencies, in ``piloop status`` and in the implementers' briefs.

An implementer sees what its predecessors did to the tail latency, not only to the primary metric.
"""

from collections import defaultdict
from statistics import fmean

from sqlalchemy import select

from piloop.capabilities import Outcome, Rendered, Rendering, Section, Surface, Table
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement


class Latency(Section):
    """The mean of each of ``metrics`` over the full benchmark, for the latest ``rows`` candidates."""

    SHOWN_IN = frozenset({Surface.STATUS, Surface.RUN})
    ORDER = 175  # after the evaluations

    metrics: list[str] = ['p50_ms', 'p95_ms']
    """The metrics of the table, as the evaluate command reports them."""
    rows: int = 5
    """How many candidates, the latest first."""

    def render(self, r: Rendering) -> Rendered | None:
        full = (
            select(Candidate.id, Candidate.verdict, Measurement.metrics)
            .join(Measurement)
            .where(
                Candidate.campaign_id == r.campaign.id,
                Measurement.evaluator_id == r.campaign.evaluator_id,
                Measurement.rung.is_(None),
                Measurement.error.is_(None),
            )
        )
        samples: dict[tuple[int, Outcome | None], list[dict[str, float]]] = defaultdict(list)
        for candidate, verdict, metrics in r.s.execute(full.order_by(Candidate.id.desc())):
            samples[candidate, verdict].append(metrics)
        if not samples:
            return None
        table = Table(
            columns=('candidate', 'verdict', *self.metrics),
            rows=tuple(
                (f'#{candidate}', verdict.value if verdict else 'pending', *(_mean(each, m) for m in self.metrics))
                for (candidate, verdict), each in list(samples.items())[: self.rows]
            ),
        )
        return Rendered(title='Latency', blocks=(table,))


def _mean(samples: list[dict[str, float]], metric: str) -> float | None:
    values = [s[metric] for s in samples if metric in s]
    return round(fmean(values), 2) if values else None
