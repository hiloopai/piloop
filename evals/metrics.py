"""Turn a run's export into its ``Result``: the heads over $ and time, the funnel, and the coordination counts.

A piloop arm's heads are its keeps, at the time of their verdict. A plain arm's heads are the commits of ``main``'s
first-parent line, at their commit time: it was told to commit only what beats its best.
"""

import json
import sqlite3
import subprocess
import tempfile
from bisect import bisect_right
from datetime import UTC, datetime
from pathlib import Path

from evals.model import Funnel, Point, Result, Sample
from evals.score import Score

LEADS = ('orchestrator', 'lead')
"""The roles whose tokens are the lead's share."""


def at(spend: list[Sample], hours: float) -> float:
    """The $ spent at ``hours``: linear between the samples around it, the last one after them."""
    times = [s.hours for s in spend]
    i = bisect_right(times, hours)
    if i == 0:
        return spend[0].usd if spend else 0.0
    if i == len(spend):
        return spend[-1].usd
    a, b = spend[i - 1], spend[i]
    return a.usd + (b.usd - a.usd) * (hours - a.hours) / (b.hours - a.hours)


def hours_since(started: datetime, stamp: str) -> float:
    """Hours from ``started`` to ``stamp``, a UTC time as SQLite or git writes it."""
    when = datetime.fromisoformat(stamp)
    return ((when if when.tzinfo else when.replace(tzinfo=UTC)) - started).total_seconds() / 3600


def plain_heads(bundle: Path, base: str, started: datetime) -> tuple[list[tuple[str, float]], int]:
    """The commits of ``main`` after ``base``, oldest first, with their hours; and the number of commits on any ref."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'clone', '-q', '--bare', str(bundle), tmp], check=True)
        log = subprocess.run(
            ['git', '-C', tmp, 'log', '--first-parent', '--reverse', '--format=%H %cI', f'{base}..main'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
        count = subprocess.run(
            ['git', '-C', tmp, 'rev-list', '--count', '--all', f'^{base}'], check=True, capture_output=True, text=True
        ).stdout
    heads = [(rev, hours_since(started, stamp)) for rev, stamp in zip(log[::2], log[1::2], strict=True)]
    return heads, int(count)


def piloop_heads(db: sqlite3.Connection, base: str, started: datetime) -> list[tuple[str, float]]:
    """The keeps after the baseline, in the order of their verdicts, with their hours."""
    rows = db.execute(
        """
        select c.revision, coalesce(
            (select max(e.at) from events e
             where e.target_kind = 'candidate' and e.target_id = c.id and e.kind = 'candidate.verdict'),
            c.updated_at) as at
        from candidates c where c.verdict = 'keep' and c.revision != ? order by at
        """,
        (base,),
    ).fetchall()
    return [(rev, hours_since(started, stamp)) for rev, stamp in rows]


def piloop_counts(db: sqlite3.Connection, base: str) -> tuple[Funnel, int, int, float | None]:
    """The funnel, the escalations, the people's comments, and the lead's share of the tokens."""

    def one(query: str, *args: object) -> int:
        return int(db.execute(query, args).fetchone()[0] or 0)

    funnel = Funnel(
        ideas=one('select count(*) from ideas'),
        runs=one("select count(*) from runs where role = 'implementer'"),
        submissions=one('select count(*) from candidates where verdict is not null and revision != ?', base),
        kept=one("select count(*) from candidates where verdict = 'keep' and revision != ?", base),
    )
    escalations = one("select count(*) from comments where kind = 'escalation'")
    people = one("select count(*) from comments where author like 'human%'")
    tokens = db.execute("select kind, sum(json_extract(usage, '$.total')) from agent_sessions group by kind").fetchall()
    total = sum(n or 0 for _, n in tokens)
    lead = sum(n or 0 for kind, n in tokens if kind in LEADS)
    return funnel, escalations, people, (lead / total if total else None)


def heads_of(run: Path) -> list[tuple[str, float]]:
    """The heads of a run after its base, with their hours."""
    meta = json.loads((run / 'run.json').read_text())
    started = datetime.fromisoformat(meta['started_at'])
    export = run / 'export' / 'out'
    if meta['piloop'] is None:
        return plain_heads(export / 'repo.bundle', meta['base'], started)[0]
    with sqlite3.connect(export / 'piloop.db') as db:
        return piloop_heads(db, meta['base'], started)


def result(run: Path, scores: dict[str, Score]) -> Result:
    """The ``Result`` of a run, from its export and the scores of its base and heads."""
    meta = json.loads((run / 'run.json').read_text())
    started = datetime.fromisoformat(meta['started_at'])
    spend = [Sample.model_validate_json(line) for line in (run / 'spend.jsonl').read_text().splitlines()]
    export = run / 'export' / 'out'
    base = meta['base']
    if meta['piloop'] is None:
        heads, commits = plain_heads(export / 'repo.bundle', base, started)
        sessions = int((export / 'sessions').read_text())
        funnel = Funnel(ideas=None, runs=sessions, submissions=commits, kept=len(heads))
        escalations, people, lead = 0, 0, None
    else:
        with sqlite3.connect(export / 'piloop.db') as db:
            heads = piloop_heads(db, base, started)
            funnel, escalations, people, lead = piloop_counts(db, base)
    curve = []
    for rev, hours in [(base, 0.0), *heads]:
        usd = at(spend, hours)
        if usd > meta['budget_usd']:
            break
        s = scores[rev]
        curve.append(Point(revision=rev, hours=hours, usd=usd, dev=s.dev, sealed=s.sealed, sealed_detail=s.detail))
    return Result(
        task=meta['task'],
        arm=meta['arm'],
        repeat=meta['repeat'],
        piloop=meta['piloop'],
        started_at=started,
        budget_usd=meta['budget_usd'],
        max_hours=meta['max_hours'],
        stopped=meta['stopped'],
        spend=spend,
        curve=curve,
        funnel=funnel,
        escalations=escalations,
        interventions=people,
        lead_token_share=lead,
    )
