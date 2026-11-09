"""``uv run python -m evals``: run an arm on a task, score a run, and write the report."""

from pathlib import Path

from cyclopts import App
from rich.console import Console

from evals.drive import drive
from evals.metrics import scored
from evals.model import Result, Settings, Task, load_arm
from evals.report import write
from evals.telemetry import traced

app = App(help=__doc__)


@app.command
def run(task: str, arm: str, *, repeat: int = 1, budget: float = 60, hours: float = 4) -> None:
    """Run ``arm`` on ``task`` within ``budget`` USD at list price and ``hours``, then score it.

    Args:
        task: A directory of ``tasks/``.
        arm: A file of ``arms/``, without ``.toml``, or the path of an arm's file.
        repeat: Which repeat this is: the run's directory is ``results/<task>/<arm>-<repeat>``.
        budget: The cap on models plus compute, in USD.
        hours: The cap on wall-clock time.
    """
    settings = Settings()
    with traced(settings):
        out = drive(Task.load(task), load_arm(arm), repeat, budget, hours, settings)
        scored(out, settings)


@app.command
def rescore(run: Path) -> None:
    """Score what a finished run lacks, its submissions too for a task with items, and write its ``result.json``.

    Args:
        run: The run's directory.
    """
    settings = Settings()
    with traced(settings):
        scored(run, settings)


@app.command
def report(
    *,
    out: Path = Path('evals/site'),
    note: list[str] | None = None,
    reference: list[str] | None = None,
    at_usd: list[float] | None = None,
) -> None:
    """Write one HTML page per task from every ``result.json`` under the results directory.

    Args:
        out: Where the pages go.
        note: ``<task>=<text>``: a line under the title of the task's page, such as a run that infrastructure hit.
        reference: An arm drawn dotted that never sets the plain bar, such as one from an earlier round.
        at_usd: A spend at which the headline compares the arms' sealed scores, such as 10.
    """
    settings = Settings()
    results = [Result.model_validate_json(p.read_text()) for p in sorted(settings.results.glob('*/*/result.json'))]
    notes: dict[str, list[str]] = {}
    for task, _, text in (n.partition('=') for n in note or []):
        notes.setdefault(task, []).append(text)
    for page in write(results, out, settings, notes, frozenset(reference or []), at_usd or []):
        Console().print(page)


if __name__ == '__main__':
    app()
