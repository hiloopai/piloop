"""The feedback loop of a run: how long its dev evaluations take, how often it experiments, and what each costs.

Dev evaluations come from the agents' Codex session files: a shell command that runs the task's evaluator, from the
model's call to the output that reports its exit. A command still running when its call returns is followed through
the later polls of its process. Submissions come from piloop's database: when each was submitted, measured on the
visible split, measured on the sealed split, and judged. A plain session's submissions are its commits on ``main``.
"""

import json
import re
import sqlite3
import statistics
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from evals.model import Loop

DEV = re.compile(r'python3?\s+(-u\s+)?evaluate\.py|from evaluate import|gpu-lock[^"\n]*evaluate')
READ = re.compile(r'^\s*(cat|sed|head|tail|less|rg|grep|wc|git (show|diff|log))\b')
SESSION = re.compile(r'"session_id"\s*:\s*(\d+)')
POLL = re.compile(r'session_id\s*:\s*(\d+)')
EXITED = re.compile(r'"exit_code"\s*:')


def when(stamp: str) -> datetime:
    """A UTC time from a session file or SQLite."""
    t = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def commands(text: str) -> Iterator[str]:
    """The shell commands of a Codex tool call: each ``cmd`` of its script."""
    for m in re.finditer(r'cmd\s*:\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')', text):
        yield m.group(1)[1:-1].replace('\\n', '\n')


def dev_evals(root: Path) -> list[tuple[datetime, float]]:
    """Each dev evaluation of the session files under ``root``: when it started, and how many seconds it took."""
    found = []
    for path in sorted(root.rglob('*.jsonl')):
        calls: dict[str, tuple[datetime, bool, str]] = {}
        running: dict[str, datetime] = {}
        for line in path.open(encoding='utf-8', errors='replace'):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get('payload') or {}
            kind = payload.get('type')
            if kind == 'custom_tool_call':
                text = str(payload.get('input') or '')
                dev = any(DEV.search(c) and not READ.match(c) for c in commands(text))
                calls[str(payload.get('call_id'))] = (when(rec['timestamp']), dev, text)
            elif kind == 'custom_tool_call_output' and str(payload.get('call_id')) in calls:
                start, dev, text = calls.pop(str(payload.get('call_id')))
                out, end = str(payload.get('output') or ''), when(rec['timestamp'])
                for polled in POLL.findall(text):
                    if polled in running and EXITED.search(out):
                        found.append((running[polled], (end - running.pop(polled)).total_seconds()))
                if dev:
                    started = SESSION.search(out)  # a command of the call still runs: the evaluation, most likely
                    if started:
                        running[started.group(1)] = start
                    else:
                        found.append((start, (end - start).total_seconds()))
    return sorted(found)


def quantile(xs: list[float], q: float) -> float | None:
    """The ``q`` quantile of ``xs``, or None."""
    if not xs:
        return None
    return statistics.quantiles(xs, n=10)[int(q * 10) - 1] if len(xs) > 1 else xs[0]


def submissions(db: Path) -> tuple[list[datetime], list[float], list[float], list[float]]:
    """When each version was submitted, and the seconds to its verdict, its visible and its sealed measurement."""
    con = sqlite3.connect(db)
    submitted = {
        json.loads(p)['candidate']: when(at)
        for at, p in con.execute("select at, payload from events where kind = 'run.submitted'")
    }
    if not submitted:  # a campaign that scores every commit: a version is submitted when it is made
        submitted = {cid: when(at) for cid, at in con.execute('select id, created_at from candidates where id > 1')}
    judged = dict(
        con.execute("select target_id, min(at) from events where kind = 'candidate.judged' group by target_id")
    )
    measured: dict[tuple[int, int], str] = {}
    for cid, evaluator, at in con.execute(
        'select candidate_id, evaluator_id, min(created_at) from measurements group by 1, 2'
    ):
        measured[(cid, evaluator)] = at
    verdict, visible, sealed = [], [], []
    for cid, at in submitted.items():
        if cid in judged:
            verdict.append((when(judged[cid]) - at).total_seconds())
        if (cid, 1) in measured:
            visible.append((when(measured[(cid, 1)]) - at).total_seconds())
            if (cid, 2) in measured:
                sealed.append((when(measured[(cid, 2)]) - when(measured[(cid, 1)])).total_seconds())
    return sorted(submitted.values()), verdict, visible, sealed


def loop(export: Path, plain_commits: list[datetime] | None, hours: float, usd: float) -> Loop:
    """The loop of a run, from its export. ``plain_commits``: a plain session's commits on ``main``."""
    evals = dev_evals(export)
    if plain_commits is None:
        made, verdict, visible, sealed = submissions(export / 'out' / 'piloop.db')
    else:
        made, verdict, visible, sealed = plain_commits, [], [], []
    seconds = [s for _, s in evals]
    third = len(seconds) // 3
    first, last = seconds[:third], seconds[-third:] if third else []
    drift = statistics.median(last) / statistics.median(first) if first and statistics.median(first) > 0 else None
    experiments = len(evals) + len(made)
    return Loop(
        dev_evals=len(evals),
        dev_p50_s=quantile(seconds, 0.5),
        dev_p90_s=quantile(seconds, 0.9),
        dev_per_hour=len(evals) / hours if hours else 0.0,
        submissions_per_hour=len(made) / hours if hours else 0.0,
        usd_per_experiment=usd / experiments if experiments else None,
        verdict_p50_s=quantile(verdict, 0.5),
        visible_p50_s=quantile(visible, 0.5),
        sealed_p50_s=quantile(sealed, 0.5),
        drift=drift,
    )
