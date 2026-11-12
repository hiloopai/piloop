"""``optimization``: the research model, which improves the metrics of a repo through ideas, runs and candidates."""

from typing import TYPE_CHECKING

from piloop.plugins.campaign_type.contract import CampaignType, Planner, TypeHost

if TYPE_CHECKING:  # piloop.config imports the capabilities, and the database imports piloop.config
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign
    from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
    from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
    from piloop.plugins.campaign_type.kit.runs.tables import Run
    from piloop.plugins.contribution import RouteHost, Routes


class Optimization(CampaignType):
    """``type = "optimization"``: ideas, runs that implement them, candidates that the evaluator measures."""

    def planner(self, host: TypeHost) -> Planner:
        """Its run queue, its runs' ends and its measurements."""
        from piloop.plugins.campaign_type.optimization.planner import OptimizationPlanner  # config imports this module

        return OptimizationPlanner(host, self)

    def base(self, s: Session, campaign: Campaign) -> Candidate | None:
        """The best of the first track."""
        from piloop.plugins.campaign_type.kit.pool.pool import best_of

        return best_of(s, campaign)

    def routes(self, host: RouteHost) -> Routes:
        """Its routes, and those of the components that it uses (``Serves``)."""
        from piloop.plugins.campaign_type.optimization.api import ROUTES
        from piloop.plugins.contribution import Routes

        return Routes(ROUTES)

    def settle(self, s: Session, run: Run, end: RunEnd, last: Candidate | None) -> None:
        """The idea is done after a finished run with a score, else open again (``release``)."""
        from piloop.plugins.campaign_type.kit.runs.stop import release

        release(s, run, end, last)

    def check_config(self, s: Session, campaign: Campaign) -> None:
        """Every idea's track stays (``check_tracks``)."""
        from piloop.plugins.campaign_type.kit.backlog.ideas import check_tracks

        check_tracks(s, campaign)
