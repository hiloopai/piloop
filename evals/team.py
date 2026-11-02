"""How the workers of a piloop run built on each other: keeps over another worker's head, and notes read across workers.

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


def stacking(db: sqlite3.Connection, bundle: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Keeps that took over another run's head and contain it, and those that do not, by ``head role>keep role``.

    A keep that contains the head it took over built on it; one that does not replaced it.
    """
    keeps = db.execute(
        """
        select c.revision, c.run_id, r.role from candidates c left join runs r on r.id = c.run_id
        where c.verdict = 'keep'
        order by coalesce(
            (select max(e.at) from events e
             where e.target_kind = 'candidate' and e.target_id = c.id and e.kind = 'candidate.verdict'),
            c.updated_at)
        """
    ).fetchall()
    built: Counter[str] = Counter()
    replaced: Counter[str] = Counter()
    with tempfile.TemporaryDirectory() as repo:
        subprocess.run(['git', 'clone', '-q', '--bare', str(bundle), repo], check=True)
        for (head, head_run, head_role), (keep, keep_run, keep_role) in zip(keeps, keeps[1:], strict=False):
            if head_run is None or head_run == keep_run:
                continue
            ancestor = subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', head, keep], check=False)
            if ancestor.returncode not in (0, 1):
                raise RuntimeError(f'git merge-base {head} {keep} exited {ancestor.returncode}')
            (built if ancestor.returncode == 0 else replaced)[f'{head_role}>{keep_role}'] += 1
    return dict(built), dict(replaced)


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
