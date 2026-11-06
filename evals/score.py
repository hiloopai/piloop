"""Score versions of a run outside it: the task's own dev evaluator and its sealed scorer, in sandboxes without network.

A task's ``scorer`` says where: on Daytona, in a sandbox the size of an evaluator, or on Modal.

The sealed data comes from ``sealed_uri`` on the machine of the driver and goes only into these sandboxes, never into
an arm's.
"""

import io
import json
import statistics
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from daytona import Daytona
from opentelemetry import context
from opentelemetry.context import Context

from evals import daytona
from evals.model import Frozen, Settings, Task
from evals.sandbox import OPT, WORK, Box, image
from evals.telemetry import tracer


class Scoring(Frozen):
    """One sandbox's scores of one version, normalised: 0 at the starting repo, 1 at the reference."""

    dev: float | None
    sealed: float | None
    detail: str
    cpu: str
    """The vendor, family and model of the sandbox's CPU, and its AVX-512: ``AuthenticAMD 25/17 avx512``."""
    items: dict[str, bool] | None = None
    """Whether the version passed each item of the sealed scorer, by name. None: the scorer reports no items, or it
    crashed."""


class Score(Frozen):
    """One version's scores: the means over its scorings (``Task.score_repeats``), each of which is kept."""

    dev: float | None
    """The mean dev score. None: a scoring failed."""
    sealed: float | None
    """The mean sealed score. None: a scoring failed."""
    detail: str
    scorings: list[Scoring] = []
    """Each sandbox's scores, with its CPU. Empty in a score written before they were kept."""
    items: dict[str, bool] | None = None
    """Whether the version passed each sealed item in every one of its scorings: a scoring without items passed none.
    None: no scoring reported items."""

    @classmethod
    def of(cls, scorings: list[Scoring]) -> Score:
        """The means of ``scorings``, None when any of them failed; and the details of each."""

        def mean(values: list[float | None]) -> float | None:
            known = [v for v in values if v is not None]
            return statistics.fmean(known) if known and len(known) == len(values) else None

        detail = '; '.join(dict.fromkeys(f'{s.cpu}: {s.detail}' for s in scorings))
        names = dict.fromkeys(name for s in scorings for name in s.items or {})
        items = {name: all((s.items or {}).get(name, False) for s in scorings) for name in names}
        return cls(
            dev=mean([s.dev for s in scorings]),
            sealed=mean([s.sealed for s in scorings]),
            detail=detail if len(scorings) == 1 else f'mean of {len(scorings)}: {detail}',
            scorings=scorings,
            items=items if any(s.items is not None for s in scorings) else None,
        )


def archives(bundle: Path, revisions: list[str]) -> tuple[dict[str, str], dict[str, bytes]]:
    """The tree of each revision of ``bundle``; and one revision of each of those trees, as a gzipped tar, by tree."""
    revisions = list(dict.fromkeys(revisions))
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'clone', '-q', '--bare', str(bundle), tmp], check=True)
        found = subprocess.run(
            ['git', '-C', tmp, 'rev-parse', *(f'{rev}^{{tree}}' for rev in revisions)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        trees = dict(zip(revisions, found, strict=True))
        return trees, {
            tree: subprocess.run(
                ['git', '-C', tmp, 'archive', '--format=tar.gz', rev], check=True, capture_output=True
            ).stdout
            for tree, rev in {tree: rev for rev, tree in trees.items()}.items()
        }


def sealed_data(name: str, settings: Settings) -> bytes:
    """``<sealed_uri>/<name>/`` as a gzipped tar: ``<task>`` holds the holdout, ``<task>.campaign`` a campaign's split."""
    if not settings.sealed_uri:
        raise ValueError('set EVALS_SEALED_URI: the sealed data lives outside this repo')
    bucket, _, prefix = settings.sealed_uri.removeprefix('s3://').partition('/')
    s3 = boto3.client('s3')
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=f'{prefix}/{name}/')
        for obj in listed.get('Contents', []):
            body = s3.get_object(Bucket=bucket, Key=obj['Key'])['Body'].read()
            info = tarfile.TarInfo(obj['Key'].removeprefix(f'{prefix}/{name}/'))
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def scorer_box(task: Task, settings: Settings, cloud: Daytona | None) -> Box | daytona.DaytonaBox:
    """A sandbox without network that scores one version, where the task's ``scorer`` says."""
    match task.scorer:
        case 'daytona':
            if cloud is None:
                raise ValueError(f'{task.name} is scored on Daytona: score() opens its client')
            return daytona.scorer(
                cloud, task, {'purpose': 'piloop-evals', 'task': task.name}, settings.score_timeout_minutes
            )
        case 'modal':
            return Box.create(
                settings,
                image([task], None),
                cpu=settings.score_cpu,
                memory_mb=settings.score_memory_mb,
                hours=1,
                network='none',
                memory_limit_mb=task.memory_limit_mb,
                gpu=task.gpu,
                cloud=task.cloud,
                task=task.name,
            )


def score_one(
    task: Task,
    rev: str,
    base: bytes,
    tree: bytes,
    sealed: bytes,
    settings: Settings,
    parent: Context,
    cloud: Daytona | None = None,
) -> Scoring:
    """Score one tree in a sandbox of its own, in a span under ``parent``."""
    with tracer.start_as_current_span('evals.score.tree', context=parent, attributes={'revision': rev}) as span:
        box = scorer_box(task, settings, cloud)
        try:
            for name, data in (('tree.tgz', tree), ('base.tgz', base), ('sealed.tgz', sealed)):
                box.write(f'{WORK}/{name}', data)
            run = f'python {OPT}/scorer.py {OPT}/tasks/{task.name} {OPT}/data/{task.name} {WORK}'
            box.run(run, timeout=settings.score_timeout_minutes * 60)
            out = json.loads(box.read(f'{WORK}/score.json'))
        finally:
            box.close()
        dev, verdict = out['dev'], out['sealed']
        detail = f'sealed: {verdict["status"]} {verdict["detail"]}'
        if dev is None:
            detail = f'dev: {out["dev_error"].strip()[-300:]}; {detail}'
        normalised = None if dev is None else task.normalised(dev['metrics'][task.metric])
        found = Scoring(
            dev=normalised, sealed=verdict['score'], detail=detail, cpu=out['cpu'], items=verdict.get('items')
        )
        span.set_attributes({k: v for k, v in found.model_dump(exclude={'items'}).items() if v is not None})
        return found


def attempted(
    task: Task,
    rev: str,
    base: bytes,
    tree: bytes,
    sealed: bytes,
    settings: Settings,
    parent: Context,
    cloud: Daytona | None = None,
) -> Scoring:
    """``score_one``, tried ``score_attempts`` times in all.

    A platform's API fails now and then, such as a 502 on ``snapshot.get``. A scorer that crashes reports it in its
    score, and is never tried again.
    """
    for _ in range(settings.score_attempts - 1):
        try:
            return score_one(task, rev, base, tree, sealed, settings, parent, cloud)
        except Exception:  # recorded on the attempt's span; the last attempt's error is raised
            continue
    return score_one(task, rev, base, tree, sealed, settings, parent, cloud)


def score(task: Task, bundle: Path, base: str, revisions: list[str], settings: Settings) -> Iterator[tuple[str, Score]]:
    """Score ``revisions`` of ``bundle`` against ``base``, ``score_concurrency`` sandboxes at a time.

    Each tree gets ``task.score_repeats`` sandboxes, and its score goes to every revision of it, yielded as soon as its
    last scoring ends. A tree whose scoring still fails after its attempts (``attempted``) fails only its own revisions:
    the others are yielded, then an ``ExceptionGroup`` names the failures.
    """
    with tracer.start_as_current_span('evals.score', attributes={'task': task.name, 'revisions': len(revisions)}):
        trees, tars = archives(bundle, [base, *revisions])
        sealed = sealed_data(task.name, settings)
        parent = context.get_current()
        cloud = daytona.client(settings) if task.scorer == 'daytona' else None
        wanted: dict[str, list[str]] = {}
        for rev in dict.fromkeys(revisions):
            wanted.setdefault(trees[rev], []).append(rev)
        pool = ThreadPoolExecutor(settings.score_concurrency)
        failed: dict[str, BaseException] = {}
        try:
            scorings = {
                tree: [
                    pool.submit(
                        attempted, task, revs[0], tars[trees[base]], tars[tree], sealed, settings, parent, cloud
                    )
                    for _ in range(task.score_repeats)
                ]
                for tree, revs in wanted.items()
            }
            of = {f: tree for tree, fs in scorings.items() for f in fs}
            left = {tree: len(fs) for tree, fs in scorings.items()}
            for f in as_completed(of):
                tree = of[f]
                left[tree] -= 1
                if left[tree]:
                    continue
                if errors := [e for g in scorings[tree] if (e := g.exception()) is not None]:
                    failed[tree] = errors[0]
                    continue
                done = Score.of([g.result() for g in scorings[tree]])
                for rev in wanted[tree]:
                    yield rev, done
        finally:
            pool.shutdown(cancel_futures=True)  # a caller that stops early waits only for the scorings that run
    if failed:
        lost, of_all = sum(len(wanted[tree]) for tree in failed), sum(len(revs) for revs in wanted.values())
        raise BaseExceptionGroup(f'{lost} of {of_all} versions could not be scored', list(failed.values()))
