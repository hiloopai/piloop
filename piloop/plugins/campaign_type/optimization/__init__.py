"""``optimization``: the research model, which improves the metrics of a repo through ideas, runs and candidates."""

from typing import TYPE_CHECKING

from piloop.plugins.campaign_type.contract import CampaignType, Planner, TypeHost

if TYPE_CHECKING:  # piloop.config imports the capabilities, and the database imports piloop.config
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign
    from piloop.ids import Revision


class Optimization(CampaignType):
    """``type = "optimization"``: ideas, runs that implement them, candidates that the evaluator measures."""

    def planner(self, host: TypeHost) -> Planner:
        """Its run queue, its runs' ends and its measurements."""
        from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner  # config imports this module

        return OptimizationPlanner(host)

    def fork_point(self, s: Session, source: Campaign) -> Revision:
        """The head, as a sweeping agent gets it checked out: the baseline when nothing is kept."""
        from piloop.plugins.campaign_type.kit.verdicts.judge import checkout_candidate

        return checkout_candidate(s, source).revision
