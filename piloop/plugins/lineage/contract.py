"""The lineage capability: a campaign as a typed graph, which a client draws without knowing the campaign's type.

A provider turns a campaign into a ``Graph``: its nodes, each with its parents, the spine from a root to the node that
the campaign builds on, and the spine's steps over time. The graph declares its own vocabulary: the kinds and the
statuses of its nodes, and the tracks of its scores. Each node names an entry of each, so a provider adds words of its
own without a new protocol, and a client draws a legend from the vocabulary alone.

``GET /v1/campaigns/{campaign}/lineage`` answers the graph of the campaign's instance as a ``Lineage``, at a cursor of
the event feed. ``[plugins.lineage] use`` names the instance, and ``[plugins.lineage.<name>]`` holds one. Unset, it is
the provider that is ``DEFAULT``.
"""

from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, NewType, Self

from pydantic import AwareDatetime, Field, model_validator

from piloop.api.views import View
from piloop.ids import AgentSessionId
from piloop.plugins.host import Capability, Many, Parameters, Pick, Scope, Use
from piloop.plugins.metric import Direction

if TYPE_CHECKING:  # piloop.db.models types campaigns.config with every capability: only the annotations name the rows
    from sqlalchemy.orm import Session

    from piloop.db.models import Campaign
    from piloop.plugins.contribution import Contribution

NodeId = NewType('NodeId', str)
"""A node's id, unique in its graph, such as ``candidate#12``."""
KindName = NewType('KindName', str)
"""The name of an entry of ``Graph.kinds``."""
StatusName = NewType('StatusName', str)
"""The name of an entry of ``Graph.statuses``."""
TrackKey = NewType('TrackKey', str)
"""The name of an entry of ``Graph.tracks``."""


class Tone(StrEnum):
    """How a client colours a status: the functional colour roles of GitHub's Primer design system."""

    NEUTRAL = 'neutral'
    """Nothing to act on, such as a discarded node."""
    ACCENT = 'accent'
    """In progress, such as a node that waits for its measurement."""
    SUCCESS = 'success'
    """A gain, such as a kept node."""
    ATTENTION = 'attention'
    """Worth a look, such as a lead that did not reach the spine."""
    DANGER = 'danger'
    """A failure, such as a node that crashed."""


class NodeKind(View):
    """What a node is, in the provider's words."""

    name: KindName
    """What a node's ``kind`` says, such as ``baseline``."""
    label: str
    """How a legend names it, such as ``Baseline``."""


class NodeStatus(View):
    """Where a node is, in the provider's words."""

    name: StatusName
    """What a node's ``status`` says, such as ``kept``."""
    label: str
    """How a legend names it, such as ``Kept``."""
    tone: Tone
    """How a client colours it."""


class ScoreTrack(View):
    """One axis of the scores: a node's score on it is the value of one metric."""

    name: TrackKey
    """What a key of a node's ``scores`` says."""
    metric: str
    """The metric that gives the score."""
    direction: Direction
    """Whether a higher or a lower score is better."""


class Judged(View):
    """Who judged a node, and why. The node's status says what the judgement is."""

    by: str
    """The actor's signature, such as ``daemon`` or ``orchestrator``."""
    reason: str | None


class RowLink(View):
    """A row of piloop that a client opens from a node, with the API's route for its kind."""

    kind: Literal['idea', 'run', 'candidate', 'measurement', 'comment']
    """The table of the row: ``idea``, ``run``, ``candidate``, ``measurement`` or ``comment``."""
    id: int
    """The row's id in its table."""
    label: str | None
    """One line about the row, such as an idea's title."""


class SessionLink(View):
    """An agent session that worked on a node, with the harness's own session id, so that a trace link is exact."""

    kind: Literal['agent_session']
    """Always ``agent_session``."""
    id: AgentSessionId
    """The agent session's id (``agent_sessions.id``)."""
    harness: str | None
    """The ``[plugins.harness.<name>]`` that ran it. Null when its row has none."""
    session_id: str | None
    """The harness's own session id. Null until piloop knows it (``agent_session.identified``)."""
    label: str | None
    """Who it is, such as ``implementer#12``."""


type NodeLink = Annotated[RowLink | SessionLink, Field(discriminator='kind')]
"""What a client opens from a node."""


class LineageNode(View):
    """One node of the graph."""

    id: NodeId
    """Unique in the graph, and the same in every read of it."""
    kind: KindName
    """The ``name`` of an entry of ``Graph.kinds``."""
    status: StatusName
    """The ``name`` of an entry of ``Graph.statuses``."""
    label: str
    """One line, such as the title of the idea that made it."""
    parents: list[NodeId]
    """The nodes that it derives from, each earlier in ``Graph.nodes``. Empty for a root. A client that draws a tree
    takes the first."""
    created: AwareDatetime
    """When the node was made."""
    judged: Judged | None
    """Who judged it and why, once a judgement exists."""
    scores: dict[TrackKey, float]
    """Its score on each track that has one. A track without a score is left out."""
    links: list[NodeLink]
    """What a client opens from it: rows of piloop, and agent sessions."""
    summary: str | None
    """More than the label, such as the first line of its commit."""


class SeriesPoint(View):
    """A node that moved the spine forward, and when: a chart of the spine plots the node's scores at ``at``."""

    node: NodeId
    """A node of the spine."""
    at: AwareDatetime
    """When it moved the spine forward."""


class Graph(View):
    """A campaign as its provider sees it: the vocabulary, the nodes, the spine and its steps.

    It is checked when it is made: every name refers to an entry, every parent comes before its child, and the spine
    and the series are paths of the graph. A graph that breaks a rule is a provider's bug.
    """

    kinds: list[NodeKind]
    """Every kind that a node may have, in the order of a legend."""
    statuses: list[NodeStatus]
    """Every status that a node may have, in the order of a legend."""
    tracks: list[ScoreTrack]
    """The axes of the scores. Empty when nothing is scored."""
    evaluator: str | None
    """What measured every score, in the provider's words, such as ``evaluator#3``. A new one means the scores were
    measured again. Null when there is none yet."""
    nodes: list[LineageNode]
    """Each parent before its children, such as oldest first."""
    spine: list[NodeId]
    """A path from a root to the node that the campaign builds on now: each node is a child of the one before. Empty
    when there is none yet."""
    series: list[SeriesPoint]
    """The nodes of the spine that moved it forward, in the order of the spine."""

    @model_validator(mode='after')
    def _consistent(self) -> Self:
        if errors := _errors(self):
            raise ValueError('; '.join(errors))
        return self


def _twice(names: Iterable[str], what: str) -> list[str]:
    return [f'{what} {name!r} is declared twice' for name, n in Counter(names).items() if n > 1]


def _unknown(names: Iterable[str], known: frozenset[str], what: str, where: str) -> list[str]:
    return [f'{where}: {what} {name!r} is not declared' for name in names if name not in known]


def _errors(graph: Graph) -> list[str]:
    """What breaks the rules of ``Graph``, in the order of its fields."""
    kinds = frozenset(k.name for k in graph.kinds)
    statuses = frozenset(s.name for s in graph.statuses)
    tracks = frozenset(t.name for t in graph.tracks)
    errors = [
        *_twice((k.name for k in graph.kinds), 'kind'),
        *_twice((s.name for s in graph.statuses), 'status'),
        *_twice((t.name for t in graph.tracks), 'track'),
        *_twice((n.id for n in graph.nodes), 'node'),
    ]
    seen: set[NodeId] = set()
    for node in graph.nodes:
        errors += _unknown([node.kind], kinds, 'kind', node.id)
        errors += _unknown([node.status], statuses, 'status', node.id)
        errors += _unknown(node.scores, tracks, 'track', node.id)
        errors += [f'{node.id}: parent {p!r} is not an earlier node' for p in node.parents if p not in seen]
        seen.add(node.id)
    parents = {n.id: n.parents for n in graph.nodes}
    spine = graph.spine
    errors += [f'spine: {step!r} is not a node' for step in spine if step not in parents]
    errors += [f'spine: {step!r} is not a root' for step in spine[:1] if parents.get(step)]
    errors += [
        f'spine: {b!r} is not a child of {a!r}' for a, b in pairwise(spine) if b in parents and a not in parents[b]
    ]
    order = {step: i for i, step in enumerate(spine)}
    places = [order.get(p.node) for p in graph.series]
    errors += [
        f'series: {p.node!r} is not on the spine' for p, at in zip(graph.series, places, strict=True) if at is None
    ]
    found = [at for at in places if at is not None]
    if found != sorted(found):
        errors.append('series: not in the order of the spine')
    return errors


class Lineage(View):
    """``GET /v1/campaigns/{campaign}/lineage``: the campaign's graph, at a cursor of the event feed.

    Follow ``/v1/events?after=<cursor>&campaign=<campaign>``, and read the lineage again after an event.
    """

    cursor: int
    """The id of the newest event that the graph includes."""
    provider: str
    """The instance that answered: ``[plugins.lineage] use``."""
    graph: Graph


class LineageProvider(Parameters, ABC):
    """``[plugins.lineage.<name>]``: a provider of the campaign's graph, which ``[plugins.lineage] use`` names."""

    DEFAULT: ClassVar[bool] = False
    """Whether a campaign that does not set ``[plugins.lineage] use`` uses this provider. One installed provider is.
    Only the class that sets it is a default: a subclass is not (``is_default``)."""

    @abstractmethod
    def graph(self, s: Session, campaign: Campaign) -> Graph:
        """The graph of ``campaign`` now, read with ``s`` in the transaction of the answer's cursor. It never writes.

        The campaign's config is ``campaign.config``.
        """

    def contribution(self) -> Contribution:
        """The capability's route, which every provider shares: the server mounts it once."""
        from piloop.plugins.contribution import Contribution  # here: the contributions import the daemon's loop
        from piloop.plugins.lineage.api import router  # here: the route imports the API, which imports the config

        return Contribution(router=router)


LINEAGE = Capability(
    'lineage',
    LineageProvider,
    table=Many(Scope.CAMPAIGN, use=Use(Scope.CAMPAIGN, Pick.ONE)),
    group='piloop.lineages',
    noun='lineage provider',
)
"""Every lineage provider by name: ``[plugins.lineage.<name>]``, and each provider that needs no keys. ``[plugins.
lineage] use`` names the campaign's: unset, the one that is ``DEFAULT``."""


def instance_of(campaign: Campaign) -> tuple[str, LineageProvider]:
    """The name and the instance of ``[plugins.lineage] use`` of ``campaign``."""
    plugins = campaign.config.plugins
    [name] = LINEAGE.used(plugins)
    return name, LINEAGE.instances(plugins)[name]
