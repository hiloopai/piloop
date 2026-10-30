"""What the runs' routes take and answer, and the client that calls them."""

from datetime import timedelta
from typing import Annotated

from pydantic import Field

from piloop.api.client import Client
from piloop.api.contract import (
    CancelBody,
    Params,
)
from piloop.api.views import (
    View,
)
from piloop.ids import AgentSessionId
from piloop.plugins.campaign_type.kit.runs.ids import RunEnding, RunId


class ExtendBody(Params):
    """How much more time a run's implementer gets."""

    by: Annotated[timedelta, Field(gt=timedelta(0))]
    """Added to its deadline, and to its earlier extensions."""


class EndBody(Params):
    """How a worker ends its own run."""

    how: RunEnding
    reason: str | None = None
    """Why it gives up. Required with ``gave_up``."""


class RunAnswer(View):
    """A run that a change asked to cancel, read at ``cursor``."""

    cursor: int
    run: RunId


class Extension(View):
    """A run whose implementer got more time, read at ``cursor``."""

    cursor: int
    run: RunId
    agent: AgentSessionId
    """Its implementer."""
    seconds: float
    """The implementer's extensions in all: how long it may run past its timeout."""


class RunsClient(Client):
    """The calls of the runs' routes: a run's cancel, extension and end."""

    def cancel_run(self, run: RunId, body: CancelBody) -> RunAnswer:
        return self.post(f'/v1/runs/{run}/cancel', body, RunAnswer)

    def extend_run(self, run: RunId, body: ExtendBody) -> Extension:
        return self.post(f'/v1/runs/{run}/extend', body, Extension)

    def end_run(self, body: EndBody) -> RunAnswer:
        return self.post('/v1/runs/end', body, RunAnswer)
