"""How the workers of a piloop run built on each other: keeps in the final head, races, and notes read across workers.

A worker is one run, of one role. A key ``a>b`` counts what a worker of role ``b`` did with the work of a worker of role
``a`` in another run, so two runs of one role count as two workers (``implementer>implementer``).
"""

import json
import re
import sqlite3
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

SNIPPET = 60
"""The characters at the start of a note, its whitespace collapsed, that mark it as read where they show up."""

SIGNATURE = re.compile(r'^[a-z][a-z0-9_]*#(\d+)$')
"""An agent's signature, ``<role>#<agent session id>``, as it signs a note."""


def final_keeps(db: sqlite3.Connection, bundle: Path) -> tuple[dict[str, int], dict[str, int]]:
    """The keeps of each role, and those of them that the final head contains: the gains that stacked.

    The final head is the last keep. A keep that it does not contain was dropped when another line won.
    """
    keeps = db.execute(
        """
        select c.revision, r.role from candidates c join runs r on r.id = c.run_id
        where c.verdict = 'keep'
        order by coalesce(
            (select max(e.at) from events e
             where e.target_kind = 'candidate' and e.target_id = c.id and e.kind = 'candidate.verdict'),
            c.updated_at)
        """
    ).fetchall()
    kept: Counter[str] = Counter(role for _, role in keeps)
    held: Counter[str] = Counter()
    if not keeps:
        return {}, {}
    final = keeps[-1][0]
    with tempfile.TemporaryDirectory() as repo:
        subprocess.run(['git', 'clone', '-q', '--bare', str(bundle), repo], check=True)
        for revision, role in keeps:
            ancestor = subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', revision, final], check=False)
            if ancestor.returncode not in (0, 1):
                raise RuntimeError(f'git merge-base {revision} {final} exited {ancestor.returncode}')
            held[role] += ancestor.returncode == 0
    return dict(kept), dict(held)


def final_line(db: sqlite3.Connection, heads: list[tuple[str, float]]) -> tuple[dict[str, int], dict[str, int]]:
    """A pool run's new bests by the role of their run, and its candidates on the final best's line, by role.

    The line is the final best's chain of parents, the best included, but the baseline: the gains that stacked.
    """
    role = {
        rev: r
        for rev, r in db.execute(
            'select c.revision, r.role from candidates c join runs r on r.id = c.run_id'
        ).fetchall()
    }
    bests: Counter[str] = Counter(role[rev] for rev, _ in heads if rev in role)
    if not heads:
        return {}, {}
    parent = dict(db.execute('select id, parent_id from candidates').fetchall())
    by_id = {
        c: (rev, r)
        for c, rev, r in db.execute(
            'select c.id, c.revision, r.role from candidates c left join runs r on r.id = c.run_id'
        )
    }
    final = next(c for c, (rev, _) in by_id.items() if rev == heads[-1][0])
    line: Counter[str] = Counter()
    at: int | None = final
    while at is not None:
        if (r := by_id[at][1]) is not None:
            line[r] += 1
        at = parent.get(at)
    return dict(bests), dict(line)


def races(db: sqlite3.Connection) -> int:
    """Submissions lost to a race: not kept, though they beat the version they started from.

    Another run's keep had moved the head past them by the time they were judged.
    """
    judged = db.execute(
        """
        select json_extract(e.payload, '$.parent_delta'), json_extract(e.payload, '$.best_delta'), c.verdict
        from events e join candidates c on c.id = e.target_id
        where e.kind = 'candidate.judged' and e.target_kind = 'candidate'
        """
    ).fetchall()
    return sum(1 for parent, best, verdict in judged if verdict != 'keep' and (parent or 0) > 0 and (best or 0) <= 0)


def record(line: str) -> dict[str, object]:
    """One line of a session file, or nothing when it is cut short, as the last line of a killed session can be."""
    try:
        found = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return found if isinstance(found, dict) else {}


def tool_results(path: Path) -> str:
    """Everything that the tools of a Claude Code session returned, its whitespace collapsed."""
    texts = []
    with path.open(encoding='utf-8', errors='replace') as lines:
        records = [record(line) for line in lines]
    for rec in records:
        message = rec.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        for c in content if isinstance(content, list) else []:
            if not (isinstance(c, dict) and c.get('type') == 'tool_result'):
                continue
            inner = c.get('content')
            if isinstance(inner, str):
                texts.append(inner)
            elif isinstance(inner, list):
                texts.extend(str(i.get('text', '')) for i in inner if isinstance(i, dict))
    return ' '.join(' '.join(texts).split())


def notes(db: sqlite3.Connection, export: Path) -> tuple[dict[str, int], dict[str, int]]:
    """The notes that the workers of each role wrote, and those read by a worker of another run, by ``author>reader``.

    A note counts once per reading role, when its start shows up in what the reader's tools returned, such as its brief
    or ``piloop history``.
    """
    sessions = {
        sid: (role, run, session)
        for sid, role, run, session in db.execute(
            "select id, kind, subject_id, session_id from agent_sessions where subject_kind = 'run'"
        )
    }
    written: Counter[str] = Counter()
    by_note = []
    for author, body in db.execute("select author, body from comments where kind = 'note'"):
        match = SIGNATURE.match(author or '')
        if match is None or int(match[1]) not in sessions:
            continue
        role, run, _ = sessions[int(match[1])]
        written[role] += 1
        by_note.append((role, run, ' '.join(body.split())[:SNIPPET]))
    files = {p.stem: p for p in export.rglob('*.jsonl')}
    seen: dict[tuple[str, int], str] = {}
    for role, run, session in sessions.values():
        if session is not None and session in files:
            seen[role, run] = seen.get((role, run), '') + ' ' + tool_results(files[session])
    read: Counter[str] = Counter()
    for author_role, author_run, snippet in by_note:
        readers = {role for (role, run), text in seen.items() if run != author_run and snippet in text}
        for role in readers:
            read[f'{author_role}>{role}'] += 1
    return dict(written), dict(read)
