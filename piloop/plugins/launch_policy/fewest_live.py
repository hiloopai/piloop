"""``fewest_live``: the built-in launch policy."""

from piloop.ids import CampaignId
from piloop.plugins.launch_policy.contract import LaunchCase, LaunchPolicy


class FewestLive(LaunchPolicy):
    """Each free slot goes to the waiting campaign with the fewest live agents, the oldest campaign on a tie.

    A campaign with many requests so never holds back another.
    """

    def pick(self, case: LaunchCase) -> CampaignId | None:
        waiting = [share for share in case.campaigns if share.waits]
        return min(waiting, key=lambda share: (share.live, share.campaign)).campaign if waiting else None
