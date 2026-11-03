"""Turn a run's export into its ``Result``: the heads over $ and time, the funnel, and the coordination counts.

A piloop arm's heads are its keeps, at the time of their verdict, for a piloop with verdicts (before 0.6). For one with
a pool (``pool``), they are its best over time: each candidate that a score made the best, at the time of that score.
A plain arm's heads are the commits of ``main``'s first-parent line, at their commit time: it was told to commit only
what beats its best.
"""

import json
import sqlite3
import subprocess
import tempfile
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.loop import loop
from evals.model import Funnel, Point, PoolCounts, Result, Sample, SelfTest, Tokens
from evals.score import Score
from evals.team import final_keeps, final_line, notes, races

LEADS = ('orchestrator', 'lead')
"""The roles whose tokens are the lead's share."""


def between(points: list[tuple[float, float]], hours: float) -> float:
    """The value at ``hours`` of ``points``, its (hours, value) pairs in order.

    It is linear between the pairs around ``hours``, and the last value after them.
    """
    i = bisect_right([h for h, _ in points], hours)
    if i == 0:
        return points[0][1] if points else 0.0
    if i == len(points):
        return points[-1][1]
    (ha, a), (hb, b) = points[i - 1], points[i]
    return a + (b - a) * (hours - ha) / (hb - ha)


def at(spend: list[Sample], hours: float) -> float:
    """The $ spent at ``hours``: linear between the samples around it, the last one after them."""
    return between([(s.hours, s.usd) for s in spend], hours)


def tokens_at(spend: list[Sample], hours: float) -> Tokens | None:
    """The tokens spent by ``hours``, as ``at``, rounded down. None: a sample from before the driver kept them."""
    counts = [(s.hours, s.tokens.model_dump()) for s in spend if s.tokens is not None]
    if not counts or len(counts) < len(spend):
        return None
    return Tokens(**{k: int(between([(h, c[k]) for h, c in counts], hours)) for k in Tokens.model_fields})


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


def pool(db: sqlite3.Connection) -> bool:
    """Whether the run's piloop has the pool (0.6): its candidates have no verdict."""
    return 'verdict' not in {row[1] for row in db.execute('pragma table_info(candidates)')}


def piloop_heads(db: sqlite3.Connection, base: str, started: datetime) -> list[tuple[str, float]]:
    """The heads after the baseline, in order, with their hours: the keeps, or the pool's best over time."""
    if pool(db):
        return pool_heads(db, base, started)
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


def pool_heads(db: sqlite3.Connection, base: str, started: datetime) -> list[tuple[str, float]]:
    """The best of a pool run over time: each change of the best that a ``candidate.scored`` records, oldest first.

    Every score names the best of its track at that moment. The tasks have one track.
    """
    revision = dict(db.execute('select id, revision from candidates').fetchall())
    heads: list[tuple[str, float]] = []
    last = base
    for stamp, best in db.execute(
        "select at, json_extract(payload, '$.best') from events where kind = 'candidate.scored' order by id"
    ):
        if best is None or (rev := revision[best]) == last:
            continue
        last = rev
        if rev != base:
            heads.append((rev, hours_since(started, stamp)))
    return heads


def pool_counts(db: sqlite3.Connection) -> PoolCounts:
    """A pool run's confirmations, sealed scorings and runs from another base."""
    sealed = [i for i, d in db.execute('select id, definition from evaluators') if 'sealed' in json.loads(d)]
    marks = ','.join('?' * len(sealed)) or 'null'
    confirmations = db.execute(
        """
        select count(*) from (select m.candidate_id from measurements m join campaigns p on p.evaluator_id = m.evaluator_id
        join candidates c on c.id = m.candidate_id
        where c.origin != 'baseline' and m.error is null and m.rung is null group by m.candidate_id having count(*) > 1)
        """
    ).fetchone()[0]
    scorings = db.execute(f'select count(*) from measurements where evaluator_id in ({marks})', sealed).fetchone()[0]
    based = db.execute(
        "select count(*) from events where kind = 'run.base_chosen'"
        " and json_extract(payload, '$.candidate') != json_extract(payload, '$.was')"
    ).fetchone()[0]
    return PoolCounts(confirmations=confirmations, sealed_scorings=scorings, based_runs=based)


def piloop_counts(db: sqlite3.Connection, base: str) -> tuple[Funnel, int, int, float | None]:
    """The funnel, the escalations, the people's comments, and the lead's share of the tokens."""

    def one(query: str, *args: object) -> int:
        return int(db.execute(query, args).fetchone()[0] or 0)

    if pool(db):
        scored = "select distinct target_id from events where kind = 'candidate.scored' and target_kind = 'candidate'"
        submissions = one(f'select count(*) from candidates where id in ({scored}) and revision != ?', base)
        kept = len(pool_heads(db, base, datetime.now(UTC)))
    else:
        submissions = one('select count(*) from candidates where verdict is not null and revision != ?', base)
        kept = one("select count(*) from candidates where verdict = 'keep' and revision != ?", base)
    funnel = Funnel(
        ideas=one('select count(*) from ideas'),
        runs=one('select count(*) from runs'),
        submissions=submissions,
        kept=kept,
    )
    escalations = one("select count(*) from comments where kind = 'escalation'")
    people = one("select count(*) from comments where author like 'human%'")
    tokens = db.execute("select kind, sum(json_extract(usage, '$.total')) from agent_sessions group by kind").fetchall()
    total = sum(n or 0 for _, n in tokens)
    lead = sum(n or 0 for kind, n in tokens if kind in LEADS)
    return funnel, escalations, people, (lead / total if total else None)


def restarts(export: Path) -> int:
    """The restarts of a supervised daemon: one line each in ``restarts``."""
    found = export / 'restarts'
    return len(found.read_text().splitlines()) if found.exists() else 0


def messages(db: sqlite3.Connection) -> tuple[int, int, int]:
    """Messages written to workers, delivered to them, and not delivered: refused or never sent."""
    count = dict(db.execute("select kind, count(*) from events where kind like 'comment.%' group by kind").fetchall())
    undelivered = count.get('comment.undelivered', 0) + count.get('comment.unsent', 0)
    return count.get('comment.message', 0), count.get('comment.delivered', 0), undelivered


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
        made: list[datetime] | None = [started + timedelta(hours=h) for _, h in heads]
        sessions = int((export / 'sessions').read_text())
        funnel = Funnel(ideas=None, runs=sessions, submissions=commits, kept=len(heads))
        sent = delivered = refused = None
        escalations, people, lead = 0, 0, None
        kept, held, line, written, read, raced, counted = {}, {}, {}, {}, {}, None, None
    else:
        with sqlite3.connect(export / 'piloop.db') as db:
            heads = piloop_heads(db, base, started)
            funnel, escalations, people, lead = piloop_counts(db, base)
            sent, delivered, refused = messages(db)
            if pool(db):
                (kept, line), held, raced, counted = final_line(db, heads), {}, None, pool_counts(db)
            else:
                kept, held = final_keeps(db, export / 'repo.bundle')
                line, raced, counted = {}, races(db), None
            written, read = notes(db, run / 'export')
        made = None
    curve = []
    for rev, hours in [(base, 0.0), *heads]:
        usd = at(spend, hours)
        if usd > meta['budget_usd']:
            break
        s = scores[rev]
        models = between([(x.hours, x.models_usd) for x in spend], hours)
        curve.append(
            Point(
                revision=rev,
                hours=hours,
                usd=usd,
                models_usd=models,
                tokens=tokens_at(spend, hours),
                dev=s.dev,
                sealed=s.sealed,
                sealed_detail=s.detail,
            )
        )
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
        restarts=restarts(export) if meta.get('supervised') else None,
        messages_sent=sent,
        messages_delivered=delivered,
        messages_refused=refused,
        selftest=SelfTest.model_validate_json(test.read_text()) if (test := run / 'selftest.json').exists() else None,
        keeps=kept,
        keeps_in_final=held,
        on_final_line=line,
        pool=counted,
        races=raced,
        notes_written=written,
        notes_read=read,
        loop=loop(run / 'export', made, spend[-1].hours if spend else 0.0, spend[-1].usd if spend else 0.0),
    )
