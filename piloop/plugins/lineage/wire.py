"""The client of the lineage's route, for a UI: ``LineageClient.lineage``. The models are in ``contract``."""

from piloop.api.client import Client, segment
from piloop.api.contract import Params
from piloop.plugins.lineage.contract import Lineage


class LineageClient(Client):
    """``Client``, with the lineage's route."""

    def lineage(self, campaign: str) -> Lineage:
        return self._get(f'/v1/campaigns/{segment(campaign)}/lineage', Params(), Lineage)
