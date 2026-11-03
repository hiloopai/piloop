"""Score versions of a run outside it: the task's own dev evaluator and its sealed scorer, in sandboxes without network.

The sealed data comes from ``sealed_uri`` on the machine of the driver and goes only into these sandboxes, never into
an arm's.
"""

import io
import json
import statistics
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from opentelemetry import context
from opentelemetry.context import Context

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


class Score(Frozen):
    """One version's scores: the means over its scorings (``Task.score_repeats``), each of which is kept."""

    dev: float | None
    """The mean dev score. None: a scoring failed."""
    sealed: float | None
    """The mean sealed score. None: a scoring failed."""
    detail: str
    scorings: list[Scoring] = []
    """Each sandbox's scores, with its CPU. Empty in a score written before they were kept."""

    @classmethod
    def of(cls, scorings: list[Scoring]) -> Score:
        """The means of ``scorings``, None when any of them failed; and the details of each."""

        def mean(values: list[float | None]) -> float | None:
            known = [v for v in values if v is not None]
            return statistics.fmean(known) if known and len(known) == len(values) else None

        detail = '; '.join(dict.fromkeys(f'{s.cpu}: {s.detail}' for s in scorings))
        return cls(
            dev=mean([s.dev for s in scorings]),
            sealed=mean([s.sealed for s in scorings]),
            detail=detail if len(scorings) == 1 else f'mean of {len(scorings)}: {detail}',
            scorings=scorings,
        )


def archives(bundle: Path, revisions: list[str]) -> dict[str, bytes]:
    """The tree of each revision of ``bundle``, as a gzipped tar."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'clone', '-q', '--bare', str(bundle), tmp], check=True)
        return {
            rev: subprocess.run(
                ['git', '-C', tmp, 'archive', '--format=tar.gz', rev], check=True, capture_output=True
            ).stdout
            for rev in dict.fromkeys(revisions)
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


def score_one(
    task: Task, rev: str, base: bytes, tree: bytes, sealed: bytes, settings: Settings, parent: Context
) -> Scoring:
    """Score one tree in a sandbox of its own, in a span under ``parent``."""
    with tracer.start_as_current_span('evals.score.tree', context=parent, attributes={'revision': rev}) as span:
        cpu, memory = settings.score_cpu, settings.score_memory_mb
        img = image([task], None)
        box = Box.create(
            settings,
            img,
            cpu=cpu,
            memory_mb=memory,
            hours=1,
            network=False,
            gpu=task.gpu,
            cloud=task.cloud,
            task=task.name,
        )
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
        found = Scoring(dev=normalised, sealed=verdict['score'], detail=detail, cpu=out['cpu'])
        span.set_attributes({k: v for k, v in found.model_dump().items() if v is not None})
        return found


def score(task: Task, bundle: Path, base: str, revisions: list[str], settings: Settings) -> dict[str, Score]:
    """Score ``revisions`` of ``bundle`` against ``base``, ``score_concurrency`` sandboxes at a time.

    Each version gets ``task.score_repeats`` sandboxes.
    """
    with tracer.start_as_current_span('evals.score', attributes={'task': task.name, 'revisions': len(revisions)}):
        trees = archives(bundle, [base, *revisions])
        sealed = sealed_data(task.name, settings)
        parent = context.get_current()
        with ThreadPoolExecutor(settings.score_concurrency) as pool:
            futures = {
                rev: [
                    pool.submit(score_one, task, rev, trees[base], trees[rev], sealed, settings, parent)
                    for _ in range(task.score_repeats)
                ]
                for rev in trees
            }
            return {rev: Score.of([f.result() for f in fs]) for rev, fs in futures.items()}
