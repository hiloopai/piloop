"""``uv run python -m evals``: run an arm on a task, score a run, and write the report."""

import json
from pathlib import Path

from cyclopts import App
from rich.console import Console

from evals.drive import drive
from evals.metrics import heads_of, result
from evals.model import Result, Settings, Task, load_arm
from evals.report import write
from evals.score import Score, score
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


def scored(run: Path, settings: Settings) -> None:
    """Score what ``run`` has not scored yet, and write its ``result.json``."""
    meta = json.loads((run / 'run.json').read_text())
    task = Task.load(meta['task'])
    known = run / 'scores.json'
    scores = {k: Score.model_validate(v) for k, v in json.loads(known.read_text()).items()} if known.exists() else {}
    wanted = [rev for rev, _ in heads_of(run) if rev not in scores]
    if wanted or meta['base'] not in scores:
        scores |= score(task, run / 'export' / 'out' / 'repo.bundle', meta['base'], wanted, settings)
        known.write_text(json.dumps({k: v.model_dump() for k, v in scores.items()}, indent=2))
    (run / 'result.json').write_text(result(run, scores).model_dump_json(indent=2))


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
