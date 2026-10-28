"""What the runs' routes take and answer, and the client that calls them."""

from piloop.api.client import Client
from piloop.api.contract import (
    CancelBody,
)
from piloop.api.views import (
    View,
)
from piloop.plugins.campaign_type.kit.runs.ids import RunId


class RunAnswer(View):
    """A run that a change asked to cancel, read at ``cursor``."""

    cursor: int
    run: RunId


class RunsClient(Client):
    """The calls of the runs' route: a run's cancel."""

    def cancel_run(self, run: RunId, body: CancelBody) -> RunAnswer:
        return self.post(f'/v1/runs/{run}/cancel', body, RunAnswer)
