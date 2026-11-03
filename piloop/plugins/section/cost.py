from piloop.api.views import Figure, Figures, Rendered
from piloop.campaign.stop import gather
from piloop.plugins.fact_source.facts import (
    ATTEMPTS,
    EVALUATIONS,
    METRIC_SUMS,
    NEW_BESTS,
    per_best,
    per_evaluation,
    per_run,
    reading,
    since_best,
    worker_spend,
)
from piloop.plugins.section.contract import Rendering, Section


class Cost(Section):
    """What the campaign spent: per new best, since the latest new best, and with ``metric`` per evaluation and per attempt.

    ``spend`` names the fact of what the campaign spent so far: by default ``spend``, the sum of the ``spend`` source.
    ``metric`` names the metric that the evaluate command prints as what one evaluation costs, for example
    ``eval_cost_usd``. With no spend known and no ``metric``, there is nothing to show.
    """

    spend: str = 'spend'
    """The fact of what the campaign spent so far."""
    metric: str | None = None
    """The metric that is what one evaluation costs."""

    def render(self, r: Rendering) -> Rendered | None:
        reads = [reading(self.spend), since_best(self.spend), NEW_BESTS, EVALUATIONS, METRIC_SUMS, ATTEMPTS]
        facts = gather(r.s, r.campaign, r.campaign.config, reads, r.now)
        spent = facts.get(reading(self.spend))
        if spent is None and self.metric is None:
            return None
        blocks = [
            Figures(
                figures=(
                    Figure(label=self.spend, value=spent),
                    Figure(label='per new best', value=_cents(per_best(facts, self.spend))),
                    Figure(label='since the latest new best', value=_cents(facts.get(since_best(self.spend)))),
                )
            )
        ]
        if (cost := self.metric) is not None:
            sums = facts.get(METRIC_SUMS) or {}
            split = (
                Figure(label=f'evaluations ({cost})', value=_cents(sums.get(cost))),
                Figure(label='per evaluation', value=_cents(per_evaluation(facts, cost))),
                Figure(label='without the evaluator', value=_cents(worker_spend(facts, self.spend, cost))),
                Figure(label='per attempt', value=_cents(per_run(facts, self.spend, cost))),
            )
            blocks.append(Figures(figures=split))
        return Rendered(title='Cost', blocks=tuple(blocks))


def _cents(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
