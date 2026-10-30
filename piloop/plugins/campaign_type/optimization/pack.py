"""The research pack: the context that the daemon gives each research sweep.

Ideas written with broad context produce more keeps. The daemon builds this pack from the database and the campaign
repo when a sweep that proposes work starts. ``piloop history`` prints it too. It contains:

- the metrics with their roles, tracks, margins and floors, and the start and the head on each;
- the analysis artifacts (``context``) and the shared workspace, newest first, as file pointers
  with an outline;
- the history (``piloop.plugins.campaign_type.optimization.history``);
- the latest implementer notes, and the digests and decisions of the orchestrator and the human, by id and first
  line.

The ideas by source and the open escalations are in the campaign overview, which every sweep also reads.

The size is bounded. Each list keeps its latest rows, as many as ``Shown`` says, a comment is its first line, and files
are pointers, not inlined content.
"""

import heapq
import io
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from rich import box
from rich.console import Console, Group, RenderableType
from rich.table import Table
from rich.text import Text
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from piloop.campaign.inbox import campaign_of
from piloop.config import CampaignConfig
from piloop.db.models import Campaign, Comment
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import CommentKind
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.config import MetricRole
from piloop.plugins.campaign_type.kit.evaluation.measure import metrics
from piloop.plugins.campaign_type.kit.verdicts.judge import head
from piloop.plugins.campaign_type.kit.verdicts.verdict_rule.contract import VERDICT_RULES
from piloop.plugins.campaign_type.optimization.history import (
    BRIEF_WIDTH,
    comment_line,
    newest,
    renderables as history,
    text_row,
    titled_table,
)
from piloop.plugins.escalation.contract import DECISION
from piloop.text import HEADING, NONE, metric

WALK_ENTRIES = 20_000
"""How much of the workspace the pack lists files from: a sweep's brief never waits for a walk of all of it."""
OUTLINE_CHARS = 600
"""The maximum length of an artifact outline: its markdown headings down to level 3, on one line."""
OUTLINE_LEVELS = 3
MARKDOWN = ('.md', '.markdown')
"""The suffixes of the files that the pack outlines."""


class Shown(Protocol):
    """How many rows of each list the pack shows: the parameters of its section (``[plugins.section.pack]``)."""

    @property
    def ideas(self) -> int:
        """The newest ideas in the history."""
        ...

    @property
    def campaign_findings(self) -> int:
        """The latest agent notes on the campaign, in the history."""
        ...

    @property
    def findings(self) -> int:
        """The latest notes of the implementers on their ideas."""
        ...

    @property
    def digests(self) -> int:
        """The latest digests and decisions of the orchestrator and the human."""
        ...

    @property
    def artifacts(self) -> int:
        """The newest analysis artifacts."""
        ...

    @property
    def workspace(self) -> int:
        """The newest files of the shared workspace."""
        ...


def pack(s: Session, campaign: Campaign, cfg: CampaignConfig, *, workspace: Path | None, shown: Shown) -> str:
    """Render the pack as markdown for the brief of a sweep, with markdown tables and no colour."""
    out = io.StringIO()
    console = Console(file=out, width=BRIEF_WIDTH, color_system=None, force_terminal=False, legacy_windows=False)
    parts = renderables(s, campaign, cfg, table_box=box.MARKDOWN, heading='### ', workspace=workspace, shown=shown)
    console.print(Group(*parts))
    return '\n'.join(line.rstrip() for line in out.getvalue().splitlines()).strip() + '\n'


def renderables(
    s: Session,
    campaign: Campaign,
    cfg: CampaignConfig,
    *,
    table_box: box.Box,
    heading: str = '',
    workspace: Path | None,
    shown: Shown,
) -> list[RenderableType]:
    """Build every section in reading order. ``heading='### '`` makes the titles markdown headings."""
    ideas = newest(s, campaign, shown.ideas)
    return [
        _metrics(s, campaign, cfg, table_box, heading),
        Text(),
        *_artifacts(campaign, cfg, workspace, heading, shown),
        *history(s, campaign, cfg, table_box=table_box, heading=heading, ideas=ideas, findings=shown.campaign_findings),
        Text(),
        *_implementer_notes(s, campaign, cfg, heading, shown.findings),
        *_digests(s, campaign, heading, shown.digests),
    ]


def _metrics(s: Session, campaign: Campaign, cfg: CampaignConfig, table_box: box.Box, heading: str) -> Table:
    """Show the definition of each metric, with the value of the start and of the head."""
    evaluator_id = campaign.evaluator_id
    start = s.scalars(
        select(Candidate)
        .where(Candidate.campaign_id == campaign.id, Candidate.origin != Origin.REFERENCE)
        .order_by(Candidate.id)
        .limit(1)
    ).first()
    trunk = head(s, campaign)
    at_start = metrics(start, evaluator_id) if start is not None else {}
    at_head = metrics(trunk, evaluator_id) if trunk is not None else {}
    start_ref = f'candidate#{start.id}' if start is not None else 'no candidate'
    head_ref = f'candidate#{trunk.id}' if trunk is not None else 'no trunk keep'
    tracks = {t.primary.name: t.name for t in cfg.all_tracks}
    rule = VERDICT_RULES.selected(cfg.plugins)
    judged = ', '.join([VERDICT_RULES.name_of(rule), *(f'{k} = {v}' for k, v in rule.model_dump(mode='json').items())])
    table = titled_table(
        f'{heading}Research pack: metrics (verdict rule {judged}; start {start_ref}, head {head_ref})',
        [
            'metric',
            'role',
            *(['track'] if cfg.multitrack else []),
            'better',
            'margin',
            'floor',
            'start',
            'head',
            'gain',
        ],
        table_box,
    )
    for m in cfg.metrics:
        a, b = at_start.get(m.name), at_head.get(m.name)
        text_row(
            table,
            m.name,
            str(m.role),
            *(
                [tracks.get(m.name, 'every track' if m.role is MetricRole.GUARDRAIL else NONE)]
                if cfg.multitrack
                else []
            ),
            str(m.direction),
            _num(m.margin) if m.fraction is None else f'{m.fraction:.0%} of the bar',
            _num(m.floor),
            _num(a),
            _num(b),
            NONE if a is None or b is None else f'{m.gain(b, a):+g}',
        )
    return table


def _artifacts(
    campaign: Campaign, cfg: CampaignConfig, workspace: Path | None, heading: str, shown: Shown
) -> list[RenderableType]:
    """List the analysis artifacts in ``context``, then the shared workspace, newest first.

    A pattern that matches no file is listed, so that a renamed report shows as missing instead of disappearing.
    """
    repo = Path(campaign.path)
    found: dict[str, Path] = {}
    missing: list[str] = []
    for pattern in cfg.context:
        matches = [p for p in sorted(repo.glob(pattern)) if p.is_file()]
        missing += [] if matches else [pattern]
        found |= {str(p): p for p in matches}
    lines = _file_lines(found, shown.artifacts)
    lines += [Text(f'- {pattern}: matches no file in {repo}') for pattern in missing]
    if not cfg.context:
        lines = [Text('- None configured: `context` in piloop.toml lists them.')]
    title = f'{heading}Research pack: analysis artifacts (read before proposing; newest first)'
    sections: list[RenderableType] = [Text(title, style='bold'), *lines, Text()]
    if workspace is not None and workspace.is_dir():
        files, cut = _recent(workspace, WALK_ENTRIES)
        title = f'{heading}Research pack: shared workspace (what agents left in ./workspace/; newest first)'
        lines = _file_lines({f'./workspace/{p.relative_to(workspace)}': p for p in files}, shown.workspace)
        if cut:
            lines.append(Text(f'- (listed from the {WALK_ENTRIES:,} entries of the most recently changed directories)'))
        sections += [Text(title, style='bold'), *(lines or [Text('- Empty.')]), Text()]
    return sections


def _recent(root: Path, budget: int) -> tuple[list[Path], bool]:
    """Files from a bounded walk under ``root``, and whether it stopped short of the end.

    The walk opens the most recently changed directory next and stops after ``budget`` entries. A new file changes its
    own directory's modification time (not its parents'), so recent files are mostly found early, and a workspace of a
    million files costs a sweep's brief ``budget`` entries. A directory that goes meanwhile (the archiver prunes) is
    skipped.
    """
    queue = [(-root.stat().st_mtime, str(root))]
    files: list[Path] = []
    seen = 0
    while queue:
        _, directory = heapq.heappop(queue)
        try:
            listing = os.scandir(directory)
        except FileNotFoundError:
            continue
        with listing as entries:
            for entry in entries:  # read as it goes: a directory of a million entries costs only the budget
                if seen == budget:
                    return files, True
                seen += 1
                if entry.is_dir(follow_symlinks=False):
                    with suppress(FileNotFoundError):
                        heapq.heappush(queue, (-entry.stat(follow_symlinks=False).st_mtime, entry.path))
                elif entry.is_file():
                    files.append(Path(entry.path))
    return files, False


def _file_lines(files: dict[str, Path], shown: int) -> list[RenderableType]:
    """Make one line per file, newest first, with its size, its modification time and its markdown outline."""
    stats = {}
    for name, path in files.items():
        with suppress(FileNotFoundError):  # archived meanwhile
            stats[name] = path.stat()
    newest = sorted(stats.items(), key=lambda kv: kv[1].st_mtime, reverse=True)
    lines: list[RenderableType] = []
    for name, stat in newest[:shown]:
        path = files[name]
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime('%Y-%m-%d %H:%M')
        size, outline = f'{stat.st_size} bytes', ''
        if path.suffix.lower() in MARKDOWN:
            try:
                text = path.read_text(errors='replace')
            except FileNotFoundError:  # archived and pruned since its stat
                continue
            size, outline = f'{len(text.splitlines())} lines', _outline(text)
        lines.append(Text(f'- {name} ({size}, modified {modified} UTC)' + (f'\n  {outline}' if outline else '')))
    if len(newest) > shown:
        lines.append(Text(f'- … and {len(newest) - shown} older files.'))
    return lines


def _outline(text: str) -> str:
    """Join the headings of a markdown text, down to level 3 and outside code fences, on one line."""
    headings: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith('```'):
            fenced = not fenced
        elif not fenced and (m := HEADING.match(line)) and len(m[1]) <= OUTLINE_LEVELS:
            headings.append(line.strip())
    joined = ' · '.join(headings) or '(no headings)'
    return joined if len(joined) <= OUTLINE_CHARS else joined[: OUTLINE_CHARS - 1] + '…'


def _implementer_notes(
    s: Session, campaign: Campaign, cfg: CampaignConfig, heading: str, count: int
) -> list[RenderableType]:
    """List the latest notes of the agents that did runs on their ideas: their reports."""
    by_runs = or_(*(Comment.author.startswith(f'{name}#', autoescape=True) for name in cfg.roles.doing_runs()))
    rows = s.scalars(
        select(Comment)
        .join(Idea, (Comment.target_kind == IDEA) & (Comment.target_id == Idea.id))
        .where(Idea.campaign_id == campaign.id, Comment.kind == CommentKind.NOTE, by_runs)
        .order_by(Comment.id.desc())
        .limit(count)
    ).all()
    title = Text(f'{heading}Research pack: latest implementer notes (newest first)', style='bold')
    lines = [comment_line(c, f'{c.author} on idea#{c.target_id}') for c in rows]
    return [title, *(lines or [Text('- None yet.')]), Text()]


def _digests(s: Session, campaign: Campaign, heading: str, count: int) -> list[RenderableType]:
    """List the latest notes and decisions of the orchestrator or a human."""
    rows = s.scalars(
        select(Comment)
        .where(
            campaign_of(Comment.target_kind, Comment.target_id) == campaign.id,
            Comment.kind.in_((CommentKind.NOTE, DECISION)),
            Comment.author.not_like('%#%'),
            Comment.author != DAEMON_ACTOR,
        )
        .order_by(Comment.id.desc())
        .limit(count)
    ).all()
    title = Text(f'{heading}Research pack: orchestrator and human digests and decisions (newest first)', style='bold')
    lines = [comment_line(c, f'{c.kind} by {c.author} on {c.target}') for c in rows]
    return [title, *(lines or [Text('- None yet.')])]


def _num(value: float | None) -> str:
    return NONE if value is None else metric(value)
