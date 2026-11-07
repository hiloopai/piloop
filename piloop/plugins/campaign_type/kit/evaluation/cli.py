"""The evaluation's commands, clients of the API: ``piloop eval``, ``piloop measurement add``, ``piloop submit``."""

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, assert_never

import cyclopts
from pydantic import ValidationError
from rich.table import Table
from rich.text import Text

from piloop import deprecated
from piloop.cli import common
from piloop.cli.client import daemon_http
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.wire import (
    CancelledBy,
    EvaluationClient,
    EvaluationState,
    Report,
    RunningSince,
    SubmitBody,
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
    and in that campaign to its baseline first, then to the oldest request. A requested remeasure shows as waiting at once.
    Each row names its evaluator. One that the campaign replaced since is ``retired``: what runs finishes under it.
    """
    with EvaluationClient.connect(daemon_http()) as client:
        evaluations = client.evaluations().evaluations
    now = datetime.now(UTC)
    table = Table('candidate', 'campaign', 'evaluator', 'state', 'summary', box=None)
    for e in evaluations:
        evaluator = f'evaluator#{e.evaluator}' + (' (retired)' if e.retired else '')
        table.add_row(
            f'candidate#{e.candidate}', e.campaign, evaluator, _state(e.state, now), Text(cell(e.summary or ''))
        )
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

    It also cancels a requested remeasure. A cancel records no measurement, and it stands: the daemon does not evaluate
    the candidate again under that evaluator, not to confirm it, not on the sealed split, and not after a restart. It is
    evaluated again only when someone asks (`piloop candidate remeasure`). The candidate keeps its origin and its
    scores. A baseline that never measured pauses its campaign's measurements: ask for it, then resume.
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
    """Record a measurement of a candidate under the campaign's current evaluator. Its mean includes it at once.

    Use it for an evaluation that you ran yourself, for example the evaluate command on the candidate's commit after
    the daemon's run timed out. `metrics` takes the metrics of piloop.toml, all of them unless `error` says why the
    evaluation failed. `seconds` and `error` are optional. The output says where the candidate stands now. The
    measurement is signed as PILOOP_ACTOR.
    """
    try:
        parsed = Report.model_validate_json(report.read_text())
    except ValidationError as e:
        raise SystemExit(f'the report is not {{"metrics": {{…}}, "seconds": …, "error": …}}: {e}') from e
    with EvaluationClient.connect(daemon_http()) as client:
        added = client.add_measurement(candidate_id, parsed)
    common.console.print(f'measurement#{added.measurement} of candidate#{candidate_id}: {added.standing}', markup=False)


def submit(
    revision: Annotated[
        str | None, cyclopts.Parameter(help='The commit to submit. Default: the one that your checkout is at.')
    ] = None,
    /,
    *,
    blob: Annotated[
        str | None, cyclopts.Parameter(help='The versions that the repo of record lacks, in the CAS.', show=False)
    ] = None,
    wait: Annotated[float | None, cyclopts.Parameter(help='Ignored.', show=False)] = None,
) -> None:
    """Score a commit of your run. It returns at once: where it stands comes to you as a `[piloop]` message.

    piloop runs the campaign's evaluate command on the commit, and scores it into the campaign's pool. Keep working
    meanwhile. The message gives its score and its gain over its parent, your line's best and the campaign's best. A
    commit whose first score beats the best is evaluated again until it is confirmed, then it can be the best. A
    campaign can cap the submissions of a run (`[evaluator] submissions`).
    """
    if wait is not None:
        deprecated.until('0.7.0', 'the relay of a worker before 0.6 passes submit --wait: delete the parameter')
    if revision is None:
        from piloop.plugins.vcs.contract import VCS  # here: only this command reads a checkout; the others load no vcs

        with closing(VCS.selected({}).open(Path.cwd())) as checkout:
            revision = checkout.head().revision
    with EvaluationClient.connect(daemon_http()) as client:
        answer = client.submit(SubmitBody(revision=revision, blob=blob))
    match answer.told:
        case None:
            line = f'candidate#{answer.candidate} is queued: where it stands comes as a [piloop] message'
        case told:
            line = f'candidate#{answer.candidate}: {told}'
    common.console.print(line, markup=False, highlight=False)
