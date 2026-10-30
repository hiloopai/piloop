"""The runs' commands: ``piloop run cancel``, ``piloop run extend`` and ``piloop run end``."""

from datetime import timedelta
from typing import Annotated

import cyclopts

from piloop.cli import common
from piloop.cli.agents import WrapUp, cancel_body
from piloop.cli.client import daemon_http
from piloop.plugins.campaign_type.kit.runs.ids import RunEnding, RunId
from piloop.plugins.campaign_type.kit.runs.wire import EndBody, ExtendBody, RunsClient

run_app = cyclopts.App(name='run', help='Act on one run.')


@run_app.command(name='cancel')
def run_cancel(
    run_id: Annotated[RunId, cyclopts.Parameter(help='The id of the run.')], /, *, wrap_up: WrapUp = False
) -> None:
    """Cancel a queued or running run. The daemon acts at once.

    A queued run never starts, and a run whose implementer fails to start is not launched again. A running implementer is ended
    with every process that it started, at once or, with `--wrap-up`, after it was told to save its work and stop. The run
    closes as `cancelled`, unless the implementer ran `piloop run end` before the daemon ended it: a later end is
    refused. Its commits are scored, and its idea is open again: drop it to take it off the board.
    """
    with RunsClient.connect(daemon_http()) as client:
        client.cancel_run(run_id, cancel_body(wrap_up))
    common.console.print(f'cancel requested for run#{run_id}: the daemon acts at once')


@run_app.command(name='extend')
def run_extend(
    run_id: Annotated[RunId, cyclopts.Parameter(help='The id of the run.')],
    /,
    *,
    by: Annotated[timedelta, cyclopts.Parameter(help='How much more time, such as `1h` or `90m`.')],
) -> None:
    """Give a running run's implementer more time.

    Its timeout ends it `--by` later, also on a worker elsewhere, and a note tells it its new deadline. Extensions add up.
    """
    with RunsClient.connect(daemon_http()) as client:
        extended = client.extend_run(run_id, ExtendBody(by=by))
    common.console.print(
        f'run#{run_id}: agent_session#{extended.agent} may run {timedelta(seconds=extended.seconds)} past its timeout',
        markup=False,
    )


@run_app.command(name='end')
def run_end(
    how: Annotated[
        RunEnding, cyclopts.Parameter(help='submitted: your work is done. gave_up: you stop without a result.')
    ],
    /,
    *,
    reason: Annotated[str | None, cyclopts.Parameter(help='Why you give up, with ids. Required with gave_up.')] = None,
) -> None:
    """End your own run. Only this ends a run before its deadline.

    The daemon ends you a moment later, with every process that you started. `submitted` settles your idea as done
    once your work is judged: in a campaign that scores what you submit, only after a `piloop submit`. `gave_up` puts
    your idea back on the board, with your reason.
    """
    with RunsClient.connect(daemon_http()) as client:
        run = client.end_run(EndBody(how=how, reason=reason)).run
    common.console.print(f'run#{run} ended as {how}: the daemon ends this session in a moment', markup=False)
