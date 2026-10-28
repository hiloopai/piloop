"""The evaluation's commands, clients of the API: ``piloop eval queue|cancel`` and ``piloop measurement add``."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, assert_never

import cyclopts
from pydantic import ValidationError
from rich.table import Table
from rich.text import Text

from piloop.cli import common
from piloop.cli.client import daemon_http
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CancelledBy,
    EvaluationClient,
    EvaluationState,
    Report,
    RunningSince,
    WaitingSince,
)
from piloop.text import age, cell

eval_app = cyclopts.App(
    name='eval', help='List or cancel the evaluations that the daemon runs or that wait for a slot.'
)


@eval_app.command(name='queue')
def eval_queue() -> None:
    """List the running evaluations, then the waiting ones in the order in which they get a slot.

    The last line counts them. A free slot of the executor goes to the campaign with the fewest running evaluations,
    and in that campaign to its head first, then to the oldest request. A requested remeasure shows as waiting at once.
    """
    with EvaluationClient.connect(daemon_http()) as client:
        evaluations = client.evaluations().evaluations
    now = datetime.now(UTC)
    table = Table('candidate', 'campaign', 'state', 'summary', box=None)
    for e in evaluations:
        table.add_row(f'candidate#{e.candidate}', e.campaign, _state(e.state, now), Text(cell(e.summary or '')))
    waiting = sum(isinstance(e.state, WaitingSince) for e in evaluations)
    if evaluations:
        common.console.print(table)
    common.console.print(
        f'{len(evaluations) - waiting} running, {waiting} waiting; `piloop eval cancel <candidate>`',
        markup=False,
    )


def _state(state: EvaluationState, now: datetime) -> str:
    match state:
        case RunningSince(since=since):
            return f'running {age(since, now)}'
        case WaitingSince(since=since):
            return f'waiting {age(since, now)}'
        case CancelledBy(by=by):
            return f'cancelled by {by}'
        case _:
            assert_never(state)


@eval_app.command(name='cancel')
def eval_cancel(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate whose evaluation to cancel.')],
    /,
) -> None:
    """Cancel the evaluation of a candidate: a waiting one never starts, and the daemon stops a running one.

    It also cancels a requested remeasure. The candidate gets a crashed measurement that names you, at once, so a run's
    candidate is judged a `crash`, and a daemon restart does not run it again. It is evaluated again only when someone
    asks (`piloop candidate remeasure`). A baseline that never measured pauses its campaign, as a failed one does.
    """
    with EvaluationClient.connect(daemon_http()) as client:
        client.cancel_evaluation(candidate_id)
    common.console.print(f'candidate#{candidate_id}: its evaluation is cancelled', markup=False)


measurement_app = cyclopts.App(name='measurement', help='Record a measurement made outside the daemon.')


@measurement_app.command(name='add')
def measurement_add(
    candidate_id: Annotated[CandidateId, cyclopts.Parameter(help='The candidate that was measured.')],
    /,
    *,
    report: Annotated[
        Path,
        cyclopts.Parameter(help='A JSON file: {"metrics": {"<name>": <value>}, "seconds": <s>, "error": "<why>"}.'),
    ],
) -> None:
    """Record a measurement of a candidate under the campaign's current evaluator, and have its verdict judged again.

    Use it for an evaluation that you ran yourself, for example the evaluate command on the candidate's commit after
    the daemon's run timed out. `metrics` takes the metrics of piloop.toml, all of them unless `error` says why the
    evaluation failed. `seconds` and `error` are optional. A pending candidate is refused. When the verdict is the
    daemon's crash, discard or interesting, the daemon judges the candidate again with every measurement under the
    current evaluator, the next time it measures the campaign. A keep, and a verdict that someone set, stay. The output
    says which. The measurement is signed as PILOOP_ACTOR.
    """
    try:
        parsed = Report.model_validate_json(report.read_text())
    except ValidationError as e:
        raise SystemExit(f'the report is not {{"metrics": {{…}}, "seconds": …, "error": …}}: {e}') from e
    with EvaluationClient.connect(daemon_http()) as client:
        added = client.add_measurement(candidate_id, parsed)
    common.console.print(f'measurement#{added.measurement} of candidate#{candidate_id}: {added.effect}', markup=False)
