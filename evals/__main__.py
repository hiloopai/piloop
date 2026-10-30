"""``uv run python -m evals``: run an arm on a task, score a run, and write the report."""

from pathlib import Path

from cyclopts import App
from rich.console import Console

from evals.drive import drive
from evals.model import Result, Settings, Task, load_arm
from evals.pilot import pilot as run_pilot, scored
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
    """Score the base and the heads of a finished run, and write its ``result.json``.

    Args:
        run: The run's directory.
    """
    settings = Settings()
    with traced(settings):
        scored(run, settings)


@app.command
def pilot(
    tasks: list[str],
    *,
    arms: list[str],
    budget: float = 60,
    hours: float = 4,
    concurrency: int = 10,
    ceiling: float = 1000,
) -> None:
    """Run many arms on many tasks from this one process, under one $ ceiling for all of them.

    Args:
        tasks: Directories of ``tasks/``.
        arms: ``<arm>=<repeats>``, each arm a file of ``arms/`` or the path of an arm's file.
        budget: The cap of each run on models plus compute, in USD.
        hours: The cap of each run on wall-clock time.
        concurrency: How many runs go at once.
        ceiling: The cap on the spend of all runs together: at it, every run still going stops.
    """
    settings = Settings()
    plan = [(load_arm(name), int(n)) for name, _, n in (a.partition('=') for a in arms)]
    with traced(settings):
        run_pilot(
            [Task.load(t) for t in tasks],
            plan,
            budget=budget,
            hours=hours,
            concurrency=concurrency,
            ceiling=ceiling,
            settings=settings,
        )


@app.command
def report(*, out: Path = Path('evals/site')) -> None:
    """Write one HTML page per task from every ``result.json`` under the results directory.

    Args:
        out: Where the pages go.
    """
    settings = Settings()
    results = [Result.model_validate_json(p.read_text()) for p in sorted(settings.results.glob('*/*/result.json'))]
    for page in write(results, out):
        Console().print(page)


if __name__ == '__main__':
    app()
