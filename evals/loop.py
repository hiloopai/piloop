"""The feedback loop of a run: how long its dev evaluations take, how often it experiments, and what each costs.

Dev evaluations come from the log that ``wrap/sitecustomize.py`` writes in the run's sandbox: each run of the task's
evaluator that piloop did not start, by hand or from an agent's own script. Submissions come from piloop's database:
when each was submitted, measured on the visible split, measured on the sealed split, and judged. A plain session's
submissions are its commits on ``main``.
"""

import json
import re
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.model import Loop
from evals.sandbox import EVALUATIONS


def when(stamp: str) -> datetime:
    """A UTC time from a session file or SQLite."""
    t = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


PROTOCOL = re.compile(r'(\bpiloop\b|PILOOP_BIN)\S*\s+(idea|submit|run|escalate|sql|message|note|candidate)\b')
"""A shell command that speaks piloop's protocol: claims, proposals, submissions, run ends, notes, the database."""


@dataclass(frozen=True)
class Call:
    """One shell command of a Claude Code session.

    When it started, how long it took, and its share of the tokens of the response that asked for it.
    """

    start: datetime
    seconds: float
    command: str
    tokens: float


def claude_calls(root: Path) -> list[Call]:
    """The shell commands of the Claude Code session files under ``root``, paired with their results."""
    found = []
    for path in sorted(root.rglob('*.jsonl')):
        pending: dict[str, tuple[datetime, str, float]] = {}
        seen: set[str] = set()
        for line in path.open(encoding='utf-8', errors='replace'):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            raw = rec.get('message')
            message: dict[str, object] = raw if isinstance(raw, dict) else {}
            listed = message.get('content')
            content: list[object] = listed if isinstance(listed, list) else []
            if rec.get('type') == 'assistant':
                used = message.get('usage')
                usage: dict[str, object] = used if isinstance(used, dict) else {}
                key = str(message.get('id'))
                tokens = 0.0 if key in seen else float(sum(v for v in usage.values() if isinstance(v, int)))
                seen.add(key)
                uses = [c for c in content if isinstance(c, dict) and c.get('type') == 'tool_use']
                for c in uses:
                    command = str((c.get('input') or {}).get('command') or '')
                    pending[str(c.get('id'))] = (when(rec['timestamp']), command, tokens / len(uses))
            elif rec.get('type') == 'user':
                for c in content:
                    if isinstance(c, dict) and c.get('type') == 'tool_result' and str(c.get('tool_use_id')) in pending:
                        start, command, tokens = pending.pop(str(c.get('tool_use_id')))
                        seconds = (when(rec['timestamp']) - start).total_seconds()
                        found.append(Call(start, seconds, command, tokens))
    return found


def quantile(xs: list[float], q: float) -> float | None:
    """The ``q`` quantile of ``xs``, or None."""
    if not xs:
        return None
    return statistics.quantiles(xs, n=10)[int(q * 10) - 1] if len(xs) > 1 else xs[0]


def submissions(db: Path) -> tuple[list[datetime], list[float], list[float], list[float]]:
    """When each version was submitted, and the seconds to its verdict, its visible and its sealed measurement.

    A pool run's verdict is its first score.
    """
    with closing(sqlite3.connect(db)) as con:
        submitted = {
            json.loads(p)['candidate']: when(at)
            for at, p in con.execute("select at, payload from events where kind = 'run.submitted'")
        }
        if not submitted:  # a campaign that scores every commit: a version is submitted when it is made
            submitted = {cid: when(at) for cid, at in con.execute('select id, created_at from candidates where id > 1')}
        judged = dict(  # a verdict, or a pool's first score
            con.execute(
                "select target_id, min(at) from events where kind in ('candidate.judged', 'candidate.scored')"
                ' group by target_id'
            )
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


def evaluations(log: Path) -> list[tuple[datetime, float]]:
    """When each dev evaluation of ``log`` started, and how many seconds it took: the runs that piloop did not start."""
    if not log.exists():
        return []
    found = []
    for line in log.read_text().splitlines():
        rec = json.loads(line)
        if not rec['piloop']:
            found.append((datetime.fromtimestamp(rec['at'], UTC), float(rec['seconds'])))
    return sorted(found)


def loop(export: Path, plain_commits: list[datetime] | None, hours: float, usd: float) -> Loop:
    """The loop of a run, from its export. ``plain_commits``: a plain session's commits on ``main``."""
    calls = claude_calls(export)
    evals = evaluations(export / 'out' / EVALUATIONS)
    protocol = [c for c in calls if PROTOCOL.search(c.command)]
    all_tokens = sum(c.tokens for c in calls)
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
        protocol_calls=len(protocol) if calls else None,
        protocol_seconds=sum(c.seconds for c in protocol) if calls else None,
        protocol_token_share=sum(c.tokens for c in protocol) / all_tokens if all_tokens else None,
    )
