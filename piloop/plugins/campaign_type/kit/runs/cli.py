"""The runs' command: ``piloop run cancel``."""

from typing import Annotated

import cyclopts

from piloop.cli import common
from piloop.cli.agents import WrapUp, cancel_body
from piloop.cli.client import daemon_http
from piloop.plugins.campaign_type.kit.runs.ids import RunId
from piloop.plugins.campaign_type.kit.runs.wire import RunsClient

run_app = cyclopts.App(name='run', help='Act on one run.')


@run_app.command(name='cancel')
def run_cancel(
    run_id: Annotated[RunId, cyclopts.Parameter(help='The id of the run.')], /, *, wrap_up: WrapUp = False
) -> None:
    """Cancel a queued or running run. The daemon acts at once.

    A queued run never starts, and a run whose implementer fails to start is not launched again. A running implementer is ended
    with every process that it started, at once or, with `--wrap-up`, after it was told to save its work and stop. The run
    closes as `cancelled`, unless the implementer left its finding before the daemon ended it: a later finding is
    refused. Its commits are scored, and its idea is parked: set it `ready` to run it again.
    """
    with RunsClient.connect(daemon_http()) as client:
        client.cancel_run(run_id, cancel_body(wrap_up))
    common.console.print(f'cancel requested for run#{run_id}: the daemon acts at once')
