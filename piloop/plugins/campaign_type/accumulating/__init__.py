"""``accumulating``: work that adds up. Each accepted submission merges into a trunk, and runs start from its head.

It is the ``optimization`` type with three changes. A submission is not scored on its own: the daemon merges what
waits onto the trunk and measures the merge once (``trunk``). A run starts from the trunk, and a claim moves a run that
submitted nothing yet there (``CampaignType.base``). An idea is done only when its work is merged (``settle``). Ideas with
``after`` links are the task tree, and the first claim wins, as in ``optimization``. The lead is an orchestrator: it
writes the ideas, their links and priorities, and may make tables of its own.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from piloop.plugins.campaign_type.contract import Planner, TypeHost
from piloop.plugins.campaign_type.optimization import Optimization

if TYPE_CHECKING:  # piloop.config imports the capabilities, and the database imports piloop.config
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign
    from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
    from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
    from piloop.plugins.campaign_type.kit.runs.tables import RunClaim
    from piloop.plugins.vcs.contract import Version

ACCUMULATING: Final = 'accumulating'


class Accumulating(Optimization):
    """``type = "accumulating"``: runs whose accepted submissions merge into a trunk that every new run starts from."""

    def planner(self, host: TypeHost) -> Planner:
        """The optimization planner, whose merge job onto the trunk replaces its scoring of each submission."""
        from piloop.plugins.campaign_type.accumulating.planner import AccumulatingPlanner  # config imports this module

        return AccumulatingPlanner(host, self)

    def base(self, s: Session, campaign: Campaign) -> Candidate | None:
        """The trunk (``trunk.head``)."""
        from piloop.plugins.campaign_type.accumulating.trunk import head

        return head(s, campaign)

    def settle(self, s: Session, claim: RunClaim, end: RunEnd, submissions: Sequence[Candidate]) -> None:
        """The idea is done when a submission for it merged into the trunk, else open again, with a note.

        The run's submissions are decided by then: the run settles after each one is merged or refused.
        """
        from piloop.plugins.campaign_type.accumulating.trunk import merged
        from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
        from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
        from piloop.plugins.campaign_type.kit.runs.stop import reopen, settle_cancelled

        if not merged(s, claim.run.campaign).isdisjoint(c.id for c in submissions):
            claim.idea.status = IdeaStatus.DONE
        elif end is RunEnd.CANCELLED:
            settle_cancelled(s, claim)
        else:
            reopen(s, claim, f'none of its work for it merged into the trunk; it ended {end}')

    def answers(self, s: Session, submission: Candidate, version: Version) -> bool:
        """When the trunk holds it, else when it is the same merge: the same version onto the same trunk (``trunk``)."""
        from piloop.plugins.campaign_type.accumulating.trunk import answers

        return answers(s, submission, version)

    def check_config(self, s: Session, campaign: Campaign) -> None:
        """As ``optimization``, and one track: the trunk takes a merge on its primary."""
        from piloop.config import ConfigError

        super().check_config(s, campaign)
        if len(tracks := campaign.config.all_tracks) > 1:
            names = ', '.join(t.name for t in tracks)
            raise ConfigError(
                f'{campaign.path}/piloop.toml: an accumulating campaign has one track, and this one has {names}:'
                ' keep one primary metric'
            )
