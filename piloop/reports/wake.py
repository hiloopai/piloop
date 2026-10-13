"""Wake overviews: the most important memory, in a fixed reading budget, at the start of a session.

A session reads a short overview first and queries for more only when it needs it (the idea of OptMem's
``memo wake``). Two overviews use it. A worker's or research sweep's brief opens with the current lessons of the
repo (``lessons_overview``). The orchestrator runs ``piloop wake`` (``orchestrator_overview``): its latest journal
lines, the open inbox, the head of each active project and the current lessons. A budget is in tokens, counted as
``CHARS_PER_TOKEN`` characters each. Lines that do not fit are counted in a last line that says how to read them.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.campaign import inbox
from piloop.config import config_of
from piloop.db.models import Comment, CommentKind, Journal, Project, ProjectStatus
from piloop.reports.overview import summary

CHARS_PER_TOKEN = 4
JOURNAL_SHARE = 0.4
"""The part of ``piloop wake``'s budget for the journal. The inbox, the projects and the lessons get 0.2 each."""
OTHER_SHARE = 0.2
LESSONS_MORE = 'the lessons query under "Remember past campaigns" in the procedures'


def fit(lines: Sequence[str], budget: int) -> tuple[list[str], int]:
    """The first ``lines`` that fit in ``budget`` tokens, and how many did not."""
    used = 0
    for i, line in enumerate(lines):
        used += len(line) // CHARS_PER_TOKEN + 1
        if used > budget:
            return list(lines[:i]), len(lines) - i
    return list(lines), 0


def fitted(lines: Sequence[str], budget: int, more: str) -> list[str]:
    """``fit``, then ``… N more: <more>`` when some lines did not fit."""
    kept, rest = fit(lines, budget)
    return [*kept, *([f'… {rest} more: {more}'] if rest else [])]


def _first_line(body: str) -> str:
    return body.strip().split('\n', 1)[0]


LESSON = re.compile(r'family: (?P<family>.+?) \| status: (?P<status>\w+) \| best: (?P<best>.*)')
EFFECT = re.compile(r'[+-]?\d+(?:\.\d+)?')


class Lesson(NamedTuple):
    """The parts of a lesson's first line that rank it. A CHECK guarantees the shape; anything else ranks last."""

    family: str
    """Lower case, with runs of spaces as one: the same family across projects."""
    untested: bool
    effect: float | None
    """The size of the first number after ``best:``, whatever its sign: a gain may be a lower time. None when there is
    no number there (``none measured``)."""


def parse(first_line: str) -> Lesson:
    """The family, status and effect size of a lesson's first line."""
    m = LESSON.match(first_line)
    if m is None:
        return Lesson(' '.join(first_line.lower().split()), True, None)
    effect = EFFECT.match(m['best'])
    size = None if effect is None else abs(float(effect[0]))
    return Lesson(' '.join(m['family'].lower().split()), m['status'] == 'untested', size)


def current_lessons(s: Session, repos: Sequence[str | None]) -> list[tuple[Comment, str]]:
    """The current lesson of each family on each of ``repos``, most relevant first, with its project's name.

    A family's current lesson is the newest one of the newest campaign that has one, whichever project on the repo
    that is. Then they rank: dead or promising before untested, a larger effect before a smaller one and a known
    effect before an unknown one, a newer campaign first.
    """
    rows = s.execute(
        select(Comment, Project)
        .join(Project, Project.id == Comment.target_id)
        .where(Comment.kind == CommentKind.LESSON, Project.repo.in_([r for r in repos if r is not None]))
        .order_by(Project.created_at.desc(), Project.id.desc(), Comment.id.desc())
    ).all()
    current: dict[tuple[str | None, str], tuple[int, Lesson, Comment, str]] = {}
    for age, (comment, project) in enumerate(rows):
        lesson = parse(_first_line(comment.body))
        current.setdefault((project.repo, lesson.family), (age, lesson, comment, project.name))
    ranked = sorted(
        current.values(),
        key=lambda row: (row[1].untested, row[1].effect is None, -(row[1].effect or 0), row[0]),
    )
    return [(comment, name) for _, _, comment, name in ranked]


def _lesson_line(comment: Comment, project: str) -> str:
    return f'- [{project}] {_first_line(comment.body)} (comment#{comment.id})'


def lessons_overview(s: Session, project: Project, budget: int) -> str:
    """The block that opens a brief: the current lessons on the project's repo. Empty when off or when there are none."""
    lessons = current_lessons(s, [project.repo]) if budget else []
    if not lessons:
        return ''
    head = [
        '# What past campaigns on this repo concluded',
        '',
        'The current lesson of each mechanism family, from the newest campaign that tried it; dead or promising first,'
        ' larger effects first. Before you propose or build a mechanism, read more with the queries under "Remember'
        ' past campaigns" in the procedures.',
        '',
    ]
    return '\n'.join(fitted([*head, *(_lesson_line(c, name) for c, name in lessons)], budget, LESSONS_MORE))


def orchestrator_overview(s: Session, budget: int, now: datetime) -> str:
    """What ``piloop wake`` prints: the journal, the open inbox, the active projects and their lessons, in ``budget``."""
    if budget <= 0:
        return ''
    journal = s.scalars(select(Journal).order_by(Journal.id.desc())).all()
    newest, older = fit(
        [f'- {j.created_at:%Y-%m-%d %H:%M} {j.author}: {j.line}' for j in journal], int(budget * JOURNAL_SHARE)
    )
    journal_lines = [
        *([f'… {older} older: SELECT created_at, author, line FROM journal ORDER BY id DESC'] if older else []),
        *reversed(newest),
    ]
    items = inbox.items(s)
    inbox_lines = fitted(
        [
            f'- p{c.priority} comment#{c.id} {c.kind} on {c.target_kind}#{c.target_id} by {c.author}'
            f'{f" ({p.name})" if p else ""}: {_first_line(c.body)[:160]}'
            for c, p in items
        ],
        int(budget * OTHER_SHARE),
        'piloop inbox',
    )
    active = s.scalars(select(Project).where(Project.status == ProjectStatus.ACTIVE).order_by(Project.id)).all()
    project_lines = fitted(
        [f'- {summary(s, p, config_of(p))}' for p in active], int(budget * OTHER_SHARE), 'piloop status'
    )
    lessons = current_lessons(s, sorted({p.repo for p in active if p.repo is not None}))
    lesson_lines = fitted([_lesson_line(c, name) for c, name in lessons], int(budget * OTHER_SHARE), LESSONS_MORE)
    sections = (
        (f'## Journal, oldest to newest ({len(journal)} lines in all)', journal_lines),
        (f'## Inbox ({len(items)} open)', inbox_lines),
        (f'## Active projects ({len(active)})', project_lines),
        (f'## Current lessons on their repos ({len(lessons)})', lesson_lines),
    )
    return '\n'.join(
        [
            f'# piloop wake, {now:%Y-%m-%d %H:%M} UTC',
            *(line for title, lines in sections for line in ('', title, *(lines or ['- none']))),
        ]
    )
