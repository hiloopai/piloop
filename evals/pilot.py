"""Run many arms from one process: repeats of each arm on each task, a few at a time, under one $ ceiling.

The runs start in turn across tasks and arms, so that each batch mixes them. A run that fails is recorded and the rest go
on. When the spend of all runs together reaches the ceiling, every running run gets a ``HALT`` file and stops.
"""

import itertools
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evals.drive import HALT, drive
from evals.metrics import heads_of, result, submissions_of
from evals.model import PiloopArm, PlainArm, Sample, Settings, Task
from evals.score import Score, score


def scored(run: Path, settings: Settings) -> None:
    """Score what ``run`` has not scored yet, and write its ``result.json``.

    The base and the heads first; then, for a task whose scorer reports items for the base, every submission and its
    parent, so that ``result.json`` can count regressions. A task without items would pay for scorings that count
    nothing.
    """
    meta = json.loads((run / 'run.json').read_text())
    task, base = Task.load(meta['task']), meta['base']
    known = run / 'scores.json'
    scores = {k: Score.model_validate(v) for k, v in json.loads(known.read_text()).items()} if known.exists() else {}

    def add(revisions: list[str]) -> None:
        """Score the ``revisions`` not scored yet, and keep each score in ``scores.json`` as soon as it is made."""
        if not (wanted := [rev for rev in dict.fromkeys(revisions) if rev not in scores]):
            return
        for rev, found in score(task, run / 'export' / 'out' / 'repo.bundle', base, wanted, settings):
            scores[rev] = found
            kept = known.with_suffix('.tmp')
            kept.write_text(json.dumps({k: v.model_dump() for k, v in scores.items()}, indent=2))
            kept.replace(known)  # a driver that dies mid-write leaves the last whole file

    add([base, *(rev for rev, _ in heads_of(run))])
    if scores[base].items is not None:
        add([rev for submission in submissions_of(run) for rev in submission])
    (run / 'result.json').write_text(result(run, scores).model_dump_json(indent=2))


def spent(settings: Settings) -> float:
    """The spend of every run under the results directory, at its latest sample."""
    total = 0.0
    for log in settings.results.glob('*/*/spend.jsonl'):
        lines = log.read_text().splitlines()
        total += Sample.model_validate_json(lines[-1]).usd if lines else 0.0
    return total


def guard(settings: Settings, ceiling: float, done: threading.Event) -> None:
    """Halt every unfinished run once the spend of all runs reaches ``ceiling``."""
    while not done.wait(settings.poll_seconds):
        if (usd := spent(settings)) >= ceiling:
            for log in settings.results.glob('*/*/spend.jsonl'):
                if not (log.parent / 'run.json').exists():
                    (log.parent / HALT).write_text(f'the pilot spent ${usd:.0f}, at its ceiling of ${ceiling:.0f}')


def one(task: Task, arm: PiloopArm | PlainArm, repeat: int, budget: float, hours: float, settings: Settings) -> None:
    """Run and score one repeat; a failure is written beside the run, not raised."""
    out = settings.results / task.name / f'{arm.name}-{repeat}'
    try:
        scored(drive(task, arm, repeat, budget, hours, settings), settings)
    except Exception:  # one broken run must not end the others: its traceback is kept beside it
        out.mkdir(parents=True, exist_ok=True)
        (out / 'failed').write_text(traceback.format_exc())


def pilot(
    tasks: list[Task],
    arms: list[tuple[PiloopArm | PlainArm, int]],
    *,
    budget: float,
    hours: float,
    concurrency: int,
    ceiling: float,
    first: int = 1,
    settings: Settings,
) -> None:
    """Run each arm its number of repeats on each task, ``concurrency`` at a time, under ``ceiling`` in all.

    The repeats are numbered from ``first``, so that more repeats of an arm go beside those that ran.
    """
    turns = [
        [(task, arm, first + i) for task in tasks for arm, n in arms if i < n] for i in range(max(n for _, n in arms))
    ]
    order = list(itertools.chain.from_iterable(turns))
    done = threading.Event()
    watcher = threading.Thread(target=guard, args=(settings, ceiling, done), daemon=True)
    watcher.start()
    try:
        with ThreadPoolExecutor(concurrency) as pool:
            for task, arm, repeat in order:
                pool.submit(one, task, arm, repeat, budget, hours, settings)
    finally:
        done.set()
