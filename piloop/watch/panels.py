"""What ``piloop watch`` shows, as data: the campaigns, the header, and for each panel its rows and a row's detail.

Everything comes from the API's answers. A ``Frame`` holds what one campaign screen read: its state, its overview, its
inbox, its ideas and its lineage. A ``Panel`` turns a frame into rows (``Item``: a key and the cells), and a row into its detail,
which may call the API for more (``Api``). Nothing here knows Textual: ``piloop/watch/app.py`` draws it.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, assert_never

import httpx2 as httpx
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text

from piloop.api.client import Client, Refused
from piloop.api.contract import CommentsQuery, InboxQuery, OutputQuery, Problem, Selection
from piloop.api.views import (
    Batch,
    CampaignOverview,
    CampaignState,
    Envelope,
    Inbox,
    InvalidConfig,
    Item as InboxItem,
    LiveAgent,
    Rendered,
    thread_lines,
)
from piloop.ids import AgentSessionId, CampaignId, CampaignStatus, CommentId, TargetKind
from piloop.plugins.campaign_type.kit.backlog.ids import IDEA, IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.wire import BacklogClient, IdeaRow, Ideas, IdeasQuery
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import MEASUREMENT
from piloop.plugins.campaign_type.kit.evaluation.wire import CandidateDetail, EvaluationClient
from piloop.plugins.lineage.contract import Graph, Lineage, LineageNode, NodeId, NodeLink, RowLink
from piloop.plugins.lineage.wire import LineageClient
from piloop.settings import WatchSettings
from piloop.text import NONE, age, cell, metric

SPARKS = '▁▂▃▄▅▆▇█'
STATUS_CHARS = 40
"""How much of a stopped campaign's reason the campaigns screen shows. ``piloop status`` shows all of it."""
IDEA_ORDER = (IdeaStatus.OPEN, IdeaStatus.CLAIMED)
"""The statuses that the ideas panel shows, in this order: the board. The panel leaves out done and dropped ideas."""

type Cell = str | Text


@dataclass(frozen=True)
class Item[K]:
    """One row of a panel: the key of what it stands for, and its cells."""

    key: K
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class Api:
    """The clients of the API that watch calls, over one connection: core's, the lineage's, and the components'."""

    core: Client
    lineage: LineageClient
    backlog: BacklogClient
    evaluation: EvaluationClient

    @classmethod
    def over(cls, http: httpx.Client) -> Api:
        """Every client over the connection ``http``."""
        return cls(Client(http), LineageClient(http), BacklogClient(http), EvaluationClient(http))


@dataclass(frozen=True)
class Frame:
    """What one campaign screen read from the API, and when it draws it."""

    state: CampaignState
    sections: list[Rendered]
    """The sections of the campaign's overview: its ``[plugins.section] use``."""
    inbox: Inbox
    ideas: Ideas
    lineage: Lineage
    now: datetime


class Part(StrEnum):
    """A view of a campaign screen that only some events touch. Every event touches the state and the overview."""

    INBOX = 'inbox'
    IDEAS = 'ideas'
    LINEAGE = 'lineage'


ALSO: dict[str, frozenset[Part]] = {
    TargetKind.CAMPAIGN: frozenset(Part),
    TargetKind.COMMENT: frozenset({Part.INBOX}),
    IDEA: frozenset({Part.IDEAS}),
    CANDIDATE: frozenset({Part.LINEAGE}),
    MEASUREMENT: frozenset({Part.LINEAGE}),
}
"""What an event about a row of each kind touches, beside the campaign's state and overview."""


def touched(batch: Batch, campaign: CampaignId) -> frozenset[Part] | None:
    """Return the parts of ``campaign``'s screen that the batch touches. None: no event of it, nor of the home."""
    mine = [e for e in batch.events if e.subject.campaign in (campaign, None)]
    if not mine:
        return None
    return frozenset().union(*(ALSO.get(e.subject.kind, frozenset()) for e in mine))


def read_frame(
    api: Api, campaign: str, now: datetime, last: Frame | None = None, parts: frozenset[Part] = frozenset(Part)
) -> Frame | InvalidConfig | None:
    """Read what the campaign screen of ``campaign`` shows: the parts in ``parts``, and the others from ``last``.

    None: the home has no such campaign (yet).
    """
    try:
        [entry] = api.core.status(Selection(campaign=campaign)).campaigns
    except Refused as e:
        if e.error == Problem.UNKNOWN_CAMPAIGN:
            return None
        raise
    if isinstance(entry, InvalidConfig):
        return entry
    ref = str(entry.id)
    [overview] = api.core.overview(Selection(campaign=ref)).campaigns
    # a config that broke since the status read shows no sections, until the event of its fix reads again
    sections = overview.sections if isinstance(overview, CampaignOverview) else []
    inbox = api.core.inbox(InboxQuery(campaign=ref)) if last is None or Part.INBOX in parts else last.inbox
    ideas = api.backlog.ideas(ref, IdeasQuery(status=IDEA_ORDER)) if last is None or Part.IDEAS in parts else last.ideas
    lineage = api.lineage.lineage(ref) if last is None or Part.LINEAGE in parts else last.lineage
    return Frame(entry, sections, inbox, ideas, lineage, now)


class Panel[K](Protocol):
    """A panel's data: its title, its rows, a row's detail."""

    def title(self, frame: Frame, items: Sequence[Item[K]]) -> str:
        """Return the panel's name and the counts over ``items``, the rows shown."""
        ...

    def items(self, frame: Frame) -> list[Item[K]]:
        """Return every row, in the panel's order."""
        ...

    def detail(self, api: Api, frame: Frame, key: K) -> RenderableType:
        """Return row ``key`` in full, for the main pane. It may read more from the API."""
        ...


def header(frame: Frame) -> Text:
    """Return the campaign's name and state, and the first section of its overview: the head, by default."""
    state = frame.state
    text = Text(f'{state.name} · {status_of(state, None)}', style='bold')
    for first in frame.sections[:1]:
        text.append('\n' + ' · '.join(b.markdown() for b in first.blocks), style='dim')
    return text


def status_of(state: CampaignState | InvalidConfig, chars: int | None) -> str:
    """Return a campaign's state in a few words: stopped and why, paused and why, or its status."""
    match state:
        case CampaignState(status=CampaignStatus.STOPPED, stopped_reason=reason):
            said = f'stopped: {reason}'
        case CampaignState(paused=str(paused), paused_reason=reason):
            said = f'paused ({paused}): {reason}'
        case CampaignState(status=status) | InvalidConfig(status=status):
            said = str(status)
        case _:
            assert_never(state)
    return said if chars is None else cell(said, chars)


CAMPAIGN_COLUMNS = ('campaign', 'status', 'head', 'vs baseline', 'agents', 'open', 'inbox', 'last event')


def campaigns(
    states: Sequence[CampaignState | InvalidConfig], latest: Mapping[CampaignId, Envelope], now: datetime
) -> list[Item[str]]:
    """Return one row per campaign, active ones first: status, head, delta to baseline, agents, queue, inbox.

    ``latest`` is the last event of each campaign that the feed showed, by the campaign's id.
    """
    rows = []
    for c in sorted(states, key=lambda c: (c.status is not CampaignStatus.ACTIVE, c.id)):
        last = latest.get(c.id)
        seen = NONE if last is None or last.time is None else f'{last.kind} {age(_at(last.time), now)} ago'
        match c:
            case CampaignState():
                tracks = c.head.tracks if c.head is not None else []
                cells: tuple[Cell, ...] = (
                    ', '.join(f'{_value(t.value)} {t.metric}' for t in tracks) or NONE,
                    ', '.join(_value(t.delta, signed=True) for t in tracks) or NONE,
                    f'{len(c.agents)} agents',
                    f'{c.ideas.get("open", 0)} open',
                    f'{c.inbox} inbox',
                )
            case InvalidConfig():
                cells = (NONE,) * 5
            case _:
                assert_never(c)
        rows.append(Item(c.name, (c.name, status_of(c, STATUS_CHARS), *cells, seen)))
    return rows


def lines(*parts: str) -> Group:
    """Return plain lines that are never read as Rich markup, because bodies are data."""
    return Group(*(Text(p) for p in parts))


class InboxPanel:
    """Panel 1: the unacknowledged inbox, most urgent first. The detail is the item in full, with its thread."""

    def title(self, frame: Frame, items: Sequence[Item[CommentId]]) -> str:
        return f'Inbox {len(items)}'

    def items(self, frame: Frame) -> list[Item[CommentId]]:
        return [Item(c.id, _inbox_cells(c, frame.now)) for c in frame.inbox.items]

    def detail(self, api: Api, frame: Frame, key: CommentId) -> RenderableType:
        title, body = thread_lines(api.core.thread(key), frame.now)
        return Group(Text(title, style='bold'), Text(), lines(*body))


def _inbox_cells(c: InboxItem, now: datetime) -> tuple[Cell, ...]:
    return (f'p{c.priority}', c.kind, c.author, age(_at(c.created), now), cell(c.body))


class AgentsPanel:
    """Panel 2: the running agents. The detail is the agent's last output."""

    def __init__(self, settings: WatchSettings) -> None:
        self.settings = settings

    def title(self, frame: Frame, items: Sequence[Item[AgentSessionId]]) -> str:
        return f'Agents {len(items)}'

    def items(self, frame: Frame) -> list[Item[AgentSessionId]]:
        return [
            Item(a.id, (f'#{a.id}', a.kind, a.state, _model(a), age(a.started, frame.now), on(frame.state, a)))
            for a in frame.state.agents
        ]

    def detail(self, api: Api, frame: Frame, key: AgentSessionId) -> RenderableType:
        agent = {a.id: a for a in frame.state.agents}[key]  # the rows and the detail read one frame
        output = api.core.agent_output(agent.id, OutputQuery(lines=self.settings.agent_lines)).lines
        return Group(
            Text(
                f'{agent.kind}-{agent.id} · {_model(agent)} · {agent.state} {age(agent.started, frame.now)}'
                f' · {agent.runtime or NONE}',
                style='bold',
            ),
            lines(f'on: {on(frame.state, agent)}'),
            Text(),
            Text(f'the last {len(output)} lines it wrote', style='dim'),
            lines(*output) if output else Text(NONE),
        )


def _model(agent: LiveAgent) -> str:
    return f'{agent.harness}/{agent.model}' if agent.harness else NONE


def on(state: CampaignState, agent: LiveAgent) -> str:
    """Return what the agent works on: the idea of its run, from the campaign's open runs, else why it was launched."""
    run = next((r for r in state.runs if r.id == agent.run), None)
    if run is None:
        return cell(agent.reason or NONE)
    return 'no idea claimed yet' if run.idea is None or run.title is None else f'idea#{run.idea} {cell(run.title)}'


class IdeasPanel:
    """Panel 3: the open ideas, then the claimed ones. The detail is the idea, runs and comments."""

    def title(self, frame: Frame, items: Sequence[Item[IdeaId]]) -> str:
        counts = {status: sum(1 for i in items if i.cells[1] == status) for status in IDEA_ORDER}
        return 'Ideas ' + (' · '.join(f'{n} {status}' for status, n in counts.items() if n) or '0')

    def items(self, frame: Frame) -> list[Item[IdeaId]]:
        return [
            Item(i.id, (f'P{i.priority}', i.status, f'#{i.id}', cell(i.title), links(i))) for i in frame.ideas.ideas
        ]

    def detail(self, api: Api, frame: Frame, key: IdeaId) -> RenderableType:
        found = api.backlog.idea(key)
        notes = api.core.comments(CommentsQuery(kind=IDEA, id=key)).comments
        idea = found.idea
        body = [
            f'status {idea.status} · priority {idea.priority} · by {idea.created_by} {age(idea.created, frame.now)} ago'
            f' · links: {links(idea)}',
            '',
            'hypothesis:',
            *found.hypothesis.strip().splitlines(),
        ]
        if found.plan:
            body += ['', 'plan:', *found.plan.strip().splitlines()]
        body += ['', f'runs ({len(found.runs)}):']
        body += [
            f'- run#{r.id} {r.end or r.status} {r.harness}/{r.model}{f": {cell(r.error)}" if r.error else ""}'
            for r in found.runs
        ]
        body += ['', f'comments ({len(notes)}):']
        body += [f'- comment#{c.id} {c.kind} by {c.author}: {cell(c.body)}' for c in notes]
        return Group(Text(f'idea#{idea.id} {idea.title}', style='bold'), lines(*body))


def links(idea: IdeaRow) -> str:
    """Return an idea's links in one cell: ``after idea#1, idea#2``, or ``—``."""
    return f'after {", ".join(f"idea#{i}" for i in idea.after)}' if idea.after else NONE


class CandidatesPanel:
    """Panel 4: the lineage's nodes, newest first, under the sparkline of the spine on each track.

    The detail is the node: its judgement, its scores against its parent's, and what it links to.
    """

    def __init__(self, settings: WatchSettings) -> None:
        self.settings = settings

    def title(self, frame: Frame, items: Sequence[Item[NodeId]]) -> str:
        lines = spine_scores(frame.lineage.graph, self.settings.spark)
        return 'Candidates ' + ' · '.join(
            f'{name} {sparkline(values)}' if len(lines) > 1 else sparkline(values) for name, values in lines.items()
        )

    def items(self, frame: Frame) -> list[Item[NodeId]]:
        """Return the nodes, newest first. A node of another kind than most says its kind."""
        graph = frame.lineage.graph
        nodes = {n.id: n for n in graph.nodes}
        common = Counter(n.kind for n in graph.nodes).most_common(1)
        statuses = {s.name: s.label for s in graph.statuses}
        kinds = {k.name: k.label for k in graph.kinds}
        newest = sorted(graph.nodes, key=lambda n: n.created, reverse=True)[: self.settings.candidates]
        rows: list[Item[NodeId]] = []
        for n in newest:
            parent = nodes.get(n.parents[0]) if n.parents else None
            status = (
                statuses[n.status] if common and n.kind == common[0][0] else f'{kinds[n.kind]} · {statuses[n.status]}'
            )
            scores = [(_value(n.scores.get(t.name)), _delta(n, parent, t.name)) for t in graph.tracks]
            rows.append(
                Item(
                    n.id,
                    (
                        n.id,
                        status,
                        *(part for score in scores for part in score),
                        age(n.created, frame.now),
                        cell(n.summary or n.label),
                    ),
                )
            )
        return rows

    def detail(self, api: Api, frame: Frame, key: NodeId) -> RenderableType:
        graph = frame.lineage.graph
        nodes = {n.id: n for n in graph.nodes}
        node = nodes[key]  # the rows and the detail read one frame
        parent = nodes.get(node.parents[0]) if node.parents else None
        statuses = {s.name: s.label for s in graph.statuses}
        judged = NONE if node.judged is None else f'by {node.judged.by}: {node.judged.reason or NONE}'
        body = [
            f'{statuses[node.status]} {judged}',
            f'parent {parent.id if parent else NONE} · {age(node.created, frame.now)} ago',
            '',
            f'scores under {graph.evaluator or NONE} (vs parent):',
            *(
                f'  {t.name} ({t.metric}) {_value(node.scores.get(t.name))} ({_delta(node, parent, t.name)})'
                for t in graph.tracks
            ),
            '',
            'links:',
            *(f'  {_link(link)}' for link in node.links),
            '',
            'summary:',
            *(node.summary or NONE).strip().splitlines(),
        ]
        for link in node.links:
            if isinstance(link, RowLink) and link.kind == CANDIDATE:
                body += ['', *measured(api.evaluation.candidate(CandidateId(link.id)))]
        return Group(Text(f'{node.id} {node.label}', style='bold'), lines(*body))


def measured(found: CandidateDetail) -> list[str]:
    """Return a candidate's revision, and each of its metrics beside its parent's."""
    evaluator = NONE if found.evaluator is None else f'evaluator#{found.evaluator}'
    rows = [
        f'candidate#{found.candidate.id} · revision {found.revision[:12]}',
        f'metrics under {evaluator} (vs parent):',
    ]
    for name, value in sorted(found.metrics.items()):
        theirs = found.parent_metrics.get(name)
        delta = '' if theirs is None else f' ({metric(value - theirs, signed=True)})'
        rows.append(f'  {name} {metric(value)}{delta}')
    return rows if found.metrics else [*rows, f'  {NONE}']


def _link(link: NodeLink) -> str:
    return f'{link.kind}#{link.id}' + (f' {link.label}' if link.label else '')


def _delta(node: LineageNode, parent: LineageNode | None, track: str) -> str:
    mine = node.scores.get(track)
    theirs = None if parent is None else parent.scores.get(track)
    return NONE if mine is None or theirs is None else metric(mine - theirs, signed=True)


class StatusPanel:
    """Panel 5: the sections of the campaign's ``[plugins.section] use``, one row each. The detail is the section."""

    def title(self, frame: Frame, items: Sequence[Item[int]]) -> str:
        return f'Status {len(items)}'

    def items(self, frame: Frame) -> list[Item[int]]:
        return [Item(i, (s.title,)) for i, s in enumerate(frame.sections)]

    def detail(self, api: Api, frame: Frame, key: int) -> RenderableType:
        return Markdown(frame.sections[key].markdown())


def spine_scores(graph: Graph, points: int) -> dict[str, list[float]]:
    """Return, for each track, the score of each of the last ``points`` steps of the spine (``Graph.series``)."""
    nodes = {n.id: n for n in graph.nodes}
    steps = [nodes[p.node] for p in graph.series[-points:]]
    return {t.name: [v for n in steps if (v := n.scores.get(t.name)) is not None] for t in graph.tracks}


def sparkline(values: list[float]) -> str:
    """Return ``▁▃▅█``, scaled from the smallest value to the largest, or ``—`` for no values."""
    if not values:
        return NONE
    low, span = min(values), (max(values) - min(values)) or 1
    marks = ''.join(SPARKS[round((v - low) / span * (len(SPARKS) - 1))] for v in values)
    return f'{marks} {metric(values[0])} → {metric(values[-1])}'


def _value(value: float | None, *, signed: bool = False) -> str:
    return NONE if value is None else metric(value, signed=signed)


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)
