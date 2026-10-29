"""``piloop watch``: the campaign on one screen, with keys to act on it. A Textual app with a layout like lazygit.

It is a client of the API alone, so it works on the daemon's socket and against a remote server alike, and it shows
how to build a UI on piloop: read a view at a cursor, follow the event feed from that cursor, and read the view again
when an event touches it.

The first screen lists every campaign, one row each. ``enter`` opens a campaign, and ``piloop watch <campaign>`` starts
there. A campaign screen has a header (the first section of the campaign's overview), numbered panels on the left
(inbox, agents, ideas, candidates, status), and on the right the detail of the selected row of the focused panel. Each panel
has its own keys, which the key bar and ``?`` show. ``/`` filters the focused panel.

A key that writes calls the route that the CLI calls (ack, reply, prompt, an idea's status or priority), signed by the watcher's actor. A ``Follower``
thread reads the event feed with long polls (``GET /v1/events?after=&wait=``) and posts each batch to the app, which
reads again what the batch touched (``panels.ALSO``). A read and a detail run in a worker thread, so a slow server never
holds a key.
"""

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import ClassVar, assert_never

import httpx2 as httpx
from rich.console import RenderableType
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, HelpPanel, Input, Label, Select, Static
from textual.widgets.data_table import RowKey

from piloop.api.client import NoDaemon, Refused
from piloop.api.contract import PRIORITIES, AckBody, EventsQuery, PromptBody, ReplyBody, Selection
from piloop.api.views import Batch, CampaignState, Envelope, InvalidConfig
from piloop.ids import AgentSessionId, CampaignId, CommentId
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.wire import IdeaRow, PriorityBody, StatusBody
from piloop.plugins.lineage.contract import NodeId
from piloop.settings import WatchSettings
from piloop.watch.panels import (
    CAMPAIGN_COLUMNS,
    AgentsPanel,
    Api,
    CandidatesPanel,
    Cell,
    Frame,
    IdeasPanel,
    InboxPanel,
    Item,
    Panel,
    Part,
    StatusPanel,
    campaigns,
    header,
    read_frame,
    touched,
)

type Failure = Refused | NoDaemon | httpx.TransportError
"""Why a read of the API got no answer: the server refused it, or nothing answered."""
FAILURES = (Refused, NoDaemon, httpx.TransportError)
CSS = """
#header { height: 2; padding: 0 1; }
#body { height: 1fr; }
#left { width: 55%; }
PanelTable {
    height: auto; min-height: 3; max-height: 30%; border: round $panel; border-title-align: left;
    border-title-color: $text-muted; scrollbar-size-horizontal: 0;
}
CandidatesTable { height: 1fr; max-height: 100%; }
PanelTable:focus { border: round $accent; border-title-color: $accent; }
#main { width: 1fr; border: round $panel; border-title-color: $text-muted; padding: 0 1; }
#main:focus { border: round $accent; border-title-color: $accent; }
#filter { dock: bottom; display: none; }
#filter.shown { display: block; }
#campaigns { height: 1fr; }
ModalScreen { align: center middle; }
#dialog { width: 80%; height: auto; border: round $accent; padding: 0 1; background: $surface; }
"""


class Started(Message):
    """The cursor that the ``Follower`` follows from, and the feed's tail before it."""

    def __init__(self, cursor: int, tail: Batch | None) -> None:
        super().__init__()
        self.cursor, self.tail = cursor, tail


class Fed(Message):
    """A batch of the event feed, which the ``Follower`` read."""

    def __init__(self, batch: Batch) -> None:
        super().__init__()
        self.batch = batch


class Lost(Message):
    """A read of the ``Follower`` that failed. It reads again after ``WatchSettings.retry``, unless it was refused."""

    def __init__(self, failure: Failure) -> None:
        super().__init__()
        self.failure = failure


class Follower(threading.Thread):
    """Read a cursor of the event feed, then the feed after it until ``stop``, and post each batch to ``app``.

    The start reads a status for the cursor, and the tail of the feed before it (``WatchSettings.tail``). A read that
    gets no answer goes again after ``WatchSettings.retry``; a refused one ends the thread. A daemon thread: the server
    holds each read up to ``WatchSettings.wait``, and the process may exit meanwhile.
    """

    def __init__(self, app: Watch) -> None:
        super().__init__(daemon=True, name='piloop-watch-feed')
        self.app, self.stop = app, threading.Event()
        self.after: int | None = None
        """The cursor that the next read starts from. None until the start read it."""
        self.batches = 0
        """How many batches the app got, the start's included: a caller waits for the next with ``read_after``."""
        self._moved = threading.Condition()
        self._ended = False

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    message = self._read()
                except Refused as e:
                    self.app.post_message(Lost(e))
                    return
                except (NoDaemon, httpx.TransportError) as e:
                    if self.stop.is_set():  # the app closed the connection on its way out
                        return
                    self.app.post_message(Lost(e))
                    self.stop.wait(self.app.settings.retry.total_seconds())
                    continue
                if not self.stop.is_set():
                    self.app.post_message(message)
                with self._moved:
                    self.after = message.cursor if isinstance(message, Started) else message.batch.next
                    self.batches += 1
                    self._moved.notify_all()
        finally:
            with self._moved:
                self._ended = True
                self._moved.notify_all()

    def _read(self) -> Started | Fed:
        core, settings = self.app.api.core, self.app.settings
        if self.after is None:
            cursor = core.status(Selection()).cursor
            tail = settings.tail
            return Started(cursor, core.events(EventsQuery(after=max(0, cursor - tail), limit=tail)) if tail else None)
        return Fed(core.events(EventsQuery(after=self.after, wait=settings.wait.total_seconds())))

    def past(self, cursor: int) -> None:
        """Block until the app has the batch that reads past ``cursor``, or the follower ended.

        A caller that wrote an event, such as a test, knows then that the app has it to read.
        """
        with self._moved:
            self._moved.wait_for(lambda: (self.after or 0) >= cursor or self._ended)

    def read_after(self, batches: int) -> None:
        """Block until the app has more than ``batches`` batches, or the follower ended."""
        with self._moved:
            self._moved.wait_for(lambda: self.batches > batches or self._ended)

    @property
    def ended(self) -> bool:
        """Whether the thread ended: the app closed, or the server refused a read."""
        return self._ended


class Ask(ModalScreen[str | None]):
    """A dialog for one line of text, such as a reason or a steering message.

    ``enter`` sends it. ``esc`` drops it, and so does ``enter`` on an empty line.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'drop', 'drop')]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(escape(self.prompt))
            yield Input(id='text')

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_drop(self) -> None:
        self.dismiss(None)


DEFAULT_PRIORITY = ReplyBody.model_fields['priority'].default
"""The priority of a reply unless the dialog changes it: the API's default, which wakes the orchestrator."""


class ReplyBox(ModalScreen[tuple[str, int] | None]):
    """A dialog for a reply and its priority.

    ``enter`` in the text sends both, ``tab`` moves to the priority, and ``esc`` drops the reply.
    """

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'drop', 'drop')]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(escape(self.prompt))
            yield Input(id='text', placeholder='your reply; enter sends it')
            yield Select([(f'p{p}', p) for p in PRIORITIES], value=DEFAULT_PRIORITY, allow_blank=False, id='priority')

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss((text, self.query_one(Select).selection or DEFAULT_PRIORITY) if text else None)

    def action_drop(self) -> None:
        self.dismiss(None)


class Table(DataTable[Text]):
    """A ``DataTable`` that draws its cells again once it sized its columns to new rows.

    It sizes them when it is idle, and keeps a line that it drew before at the widths that its columns had then
    (textual 8.2.8): a row drawn between ``add_row`` and that idle stayed cut, as ``4 sc`` for ``4 score``.
    """

    def _update_dimensions(self, new_rows: Iterable[RowKey]) -> None:
        super()._update_dimensions(new_rows)
        self._clear_caches()


class Pane(Table):
    """What the campaign screen asks of each numbered panel, whatever the type of its keys."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('j', 'cursor_down', 'down', show=False),
        Binding('k', 'cursor_up', 'up', show=False),
    ]

    def __init__(self, name: str) -> None:
        super().__init__(cursor_type='row', show_header=False, id=name)
        self.query_text = ''

    def fill(self, frame: Frame) -> None:
        """Replace the rows with the panel's rows of ``frame``."""
        raise NotImplementedError

    def selected(self) -> str | None:
        """The row key of the selected row; None in an empty panel."""
        if not self.row_count:
            return None
        return str(self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value)

    def detail(self, api: Api, frame: Frame) -> RenderableType:
        """The detail of the selected row. It may read the API: call it in a worker."""
        raise NotImplementedError


class PanelTable[K](Pane):
    """One numbered panel. Its rows come from ``panel``, and the cursor stays on its key when the rows change."""

    def __init__(self, number: int, name: str, panel: Panel[K]) -> None:
        super().__init__(name)
        self.number, self.panel = number, panel
        self.shown: dict[str, Item[K]] = {}

    def fill(self, frame: Frame) -> None:
        """Replace the rows with the panel's rows that match the filter, and title the panel by what it shows.

        The cursor stays on its key, else near its index.
        """
        items = self.panel.items(frame)
        shown = [i for i in items if self.query_text.lower() in ' '.join(map(str, i.cells)).lower()]
        current, index = self.key(), self.cursor_row
        self.clear(columns=True)
        self.shown = {str(i.key): i for i in shown}
        if shown:
            self.add_columns(*(str(n) for n in range(len(shown[0].cells))))
            for item in shown:
                self.add_row(*plain(item.cells), key=str(item.key))
        keys = [i.key for i in shown]
        row = keys.index(current) if current is not None and current in keys else min(index, len(keys) - 1)
        self.move_cursor(row=row, animate=False)
        query = f' /{self.query_text}' if self.query_text else ''
        self.border_title = escape(f'[{self.number}] {self.panel.title(frame, shown)}{query}')

    def key(self) -> K | None:
        """The key of the selected row; None in an empty panel."""
        selected = self.selected()
        return None if selected is None else self.shown[selected].key

    def detail(self, api: Api, frame: Frame) -> RenderableType:
        key = self.key()
        return Text('nothing here') if key is None else self.panel.detail(api, frame, key)

    def keys(self) -> list[K]:
        """The keys of every row shown, filter applied."""
        return [item.key for item in self.shown.values()]

    @property
    def campaign(self) -> CampaignScreen:
        screen = self.screen
        assert isinstance(screen, CampaignScreen)
        return screen


class InboxTable(PanelTable[CommentId]):
    """Panel 1. ``a`` acknowledges the item, ``A`` every item shown, and ``r`` replies to its thread with a priority."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('a', 'ack', 'ack'),
        Binding('A', 'ack_shown', 'ack all shown'),
        Binding('r', 'reply', 'reply'),
    ]

    def action_ack(self) -> None:
        if (key := self.key()) is not None:
            self.ack([key])

    def action_ack_shown(self) -> None:
        if keys := self.keys():
            self.ack(keys)

    def ack(self, ids: list[CommentId]) -> None:
        core = self.campaign.api.core
        self.campaign.act(lambda: '; '.join(r.line() for r in core.ack(AckBody(ids=tuple(ids))).results))

    def action_reply(self) -> None:
        if (key := self.key()) is None:
            return

        def send(answer: tuple[str, int] | None) -> None:
            if answer is None:
                self.campaign.say('reply dropped')
                return
            text, priority = answer

            def write() -> str:
                sent = self.campaign.api.core.reply(key, ReplyBody(body=text, priority=priority)).comment
                return f'sent comment#{sent.id} (p{priority}) in reply to {sent.target}'

            self.campaign.act(write)

        self.app.push_screen(ReplyBox(f'reply to comment#{key}, as {self.campaign.main_app.actor}'), send)


class AgentsTable(PanelTable[AgentSessionId]):
    """Panel 2. ``s`` steers the agent with a message."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('s', 'steer', 'steer')]

    def action_steer(self) -> None:
        if (key := self.key()) is None:
            return

        def send(text: str | None) -> None:
            if text is not None:
                self.campaign.act(lambda: steered(self.campaign.api, key, text))

        self.app.push_screen(Ask(f'steer agent_session#{key}: a message for its next turn'), send)


def steered(api: Api, agent: AgentSessionId, text: str) -> str:
    """Queue a message for a running agent as ``piloop prompt`` does, and return a note that says so."""
    answer = api.core.prompt(agent, PromptBody(text=text))
    return f'comment#{answer.comment}: queued for agent_session#{agent}; the daemon delivers it'


class IdeasTable(PanelTable[IdeaId]):
    """Panel 3. ``y`` approves (to ``ready``), ``p`` parks and ``x`` rejects, each with a reason.

    ``+`` and ``-`` change the priority. A seed cannot be approved here: research develops it into a defined idea first
    (``piloop idea develop``), and the API says so.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('y', 'review("ready")', 'approve'),
        Binding('p', 'review("parked")', 'park'),
        Binding('x', 'review("rejected")', 'reject'),
        Binding('plus', 'priority(-1)', 'more urgent'),
        Binding('minus', 'priority(1)', 'less urgent'),
    ]

    def selected_idea(self) -> IdeaRow | None:
        frame, key = self.campaign.frame, self.key()
        return None if frame is None or key is None else next(i for i in frame.ideas.ideas if i.id == key)

    def action_review(self, status: str) -> None:
        if (idea := self.selected_idea()) is None:
            return
        to = IdeaStatus(status)
        if idea.status is to:
            self.campaign.say(f'idea#{idea.id} is {to} already')
            return

        def send(reason: str | None) -> None:
            if reason is None:
                self.campaign.say(f'idea#{idea.id} left {idea.status}: no reason given')
                return

            def write() -> str:
                changed = self.campaign.api.backlog.set_status(idea.id, StatusBody(status=to, reason=reason))
                return f'idea#{idea.id} {changed.was} → {changed.idea.status}'

            self.campaign.act(write)

        self.app.push_screen(Ask(f'idea#{idea.id} {idea.status} → {to}: why? (a note on the idea)'), send)

    def action_priority(self, by: int) -> None:
        if (idea := self.selected_idea()) is not None:
            body = PriorityBody(priority=min(max(idea.priority + by, PRIORITIES.start), PRIORITIES.stop - 1))
            backlog = self.campaign.api.backlog
            self.campaign.act(lambda: f'idea#{idea.id} priority {backlog.set_priority(idea.id, body).idea.priority}')


class CandidatesTable(PanelTable[NodeId]):
    """Panel 4, read-only: the lineage's nodes, newest first, under the sparkline of the spine on each track."""


class StatusTable(PanelTable[int]):
    """Panel 5, read-only: the sections of the campaign's overview."""


class FilterInput(Input):
    """The filter of the focused panel, opened with ``/``. ``enter`` applies it and ``esc`` clears it."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('escape', 'clear', 'clear filter')]

    def action_clear(self) -> None:
        self.value = ''
        self.post_message(self.Submitted(self, ''))


class CampaignsScreen(Screen[None]):
    """Every campaign, one row each, active campaigns first. ``enter`` opens one."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding('enter', 'open', 'open', priority=True)]

    def compose(self) -> ComposeResult:
        yield Static(id='header')
        table = Table(cursor_type='row', id='campaigns')
        table.add_columns(*CAMPAIGN_COLUMNS)
        yield table
        yield Footer()

    @property
    def main_app(self) -> Watch:
        app = self.app
        assert isinstance(app, Watch)
        return app

    def on_mount(self) -> None:
        self.reload()

    def fed(self, batch: Batch) -> None:
        self.reload()

    def reload(self) -> None:
        """Read the status of every campaign in a worker, then draw it. The next batch of the feed reads again."""
        app = self.main_app
        read(self, lambda: app.api.core.status(Selection()).campaigns, self.draw, self.failed_with)

    failed: Failure | None = None
    """Why the last read failed, until a read answers."""

    def failed_with(self, failure: Failure) -> None:
        self.failed = failure

    def draw(self, states: list[CampaignState | InvalidConfig]) -> None:
        app, self.failed = self.main_app, None
        rows = campaigns(states, app.latest, app.now())
        table = self.query_one(DataTable)
        index = table.cursor_row
        table.clear()
        for item in rows:
            table.add_row(*plain(item.cells), key=item.key)
        table.move_cursor(row=min(index, len(rows) - 1), animate=False)
        self.query_one('#header', Static).update(Text(f'piloop: {len(rows)} campaign{"" if len(rows) == 1 else "s"}'))

    def action_open(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count:
            name = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.app.push_screen(CampaignScreen(str(name)))


@dataclass(frozen=True)
class Shown:
    """What the main pane shows: the panel, the row, and the frame that it was read at."""

    table: str | None
    key: str | None
    frame: Frame | None


class CampaignScreen(Screen[None]):
    """One campaign: the header, the panels and the selected row's detail."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('1', 'panel("inbox")', 'inbox', show=False),
        Binding('2', 'panel("agents")', 'agents', show=False),
        Binding('3', 'panel("ideas")', 'ideas', show=False),
        Binding('4', 'panel("candidates")', 'candidates', show=False),
        Binding('5', 'panel("status")', 'status', show=False),
        Binding('0', 'main', 'detail', show=False),
        Binding('slash', 'filter', 'filter'),
        Binding('escape', 'campaigns', 'campaigns'),
    ]

    def __init__(self, ref: str) -> None:
        super().__init__()
        self.ref = ref
        self.note = ''
        """The last action's outcome, as the toast said it."""
        self.frame: Frame | None = None
        """What the screen last read; None until the first read answers."""
        self.shown = Shown(None, None, None)

    @property
    def main_app(self) -> Watch:
        """The ``Watch`` app. Not ``watch``: that name is Textual's method for watching a reactive attribute."""
        app = self.app
        assert isinstance(app, Watch)
        return app

    @property
    def api(self) -> Api:
        return self.main_app.api

    def compose(self) -> ComposeResult:
        settings = self.main_app.settings
        yield Static(id='header')
        with Horizontal(id='body'):
            with Vertical(id='left'):
                yield InboxTable(1, 'inbox', InboxPanel())
                yield AgentsTable(2, 'agents', AgentsPanel(settings))
                yield IdeasTable(3, 'ideas', IdeasPanel())
                yield CandidatesTable(4, 'candidates', CandidatesPanel(settings))
                yield StatusTable(5, 'status', StatusPanel())
            with VerticalScroll(id='main'):
                yield Static(id='detail')
        yield FilterInput(id='filter', placeholder='filter the focused panel; enter applies, esc clears')
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def tables(self) -> list[Pane]:
        return list(self.query(Pane))

    def fed(self, batch: Batch) -> None:
        """Read again what the batch touched (``touched``) and what a failed read left, else draw again: the ages move."""
        frame = self.frame
        if frame is None:
            self.reload()
        elif (parts := touched(batch, frame.state.id)) is not None or self.unread:
            self.reload((parts or frozenset()) | self.unread)
        else:
            self.draw(replace(frame, now=self.main_app.now()))

    def reload(self, parts: frozenset[Part] = frozenset(Part)) -> None:
        """Read the campaign in a worker, then fill every panel and the detail.

        A read that fails leaves its parts in ``unread``, which the next batch of the feed reads again.
        """
        app, last = self.main_app, self.frame

        def failed(_: Failure) -> None:
            self.unread |= parts

        read(self, lambda: read_frame(self.api, self.ref, app.now(), last, parts), self.drawn, failed)

    unread: frozenset[Part] = frozenset()
    """The parts that the last read failed to read: the next batch of the feed reads them again."""

    def drawn(self, frame: Frame | InvalidConfig | None) -> None:
        match frame:
            case None:
                self.query_one('#header', Static).update(
                    Text(f'no campaign {self.ref!r} yet: `piloop init` registers it, and this view follows')
                )
            case InvalidConfig(name=name, problem=problem, fix=fix):
                self.query_one('#header', Static).update(Text(f'{name}: {problem}; fix it with {fix}'))
            case Frame():
                first, self.unread = self.frame is None, frozenset()
                self.draw(frame)
                if first:
                    inbox = self.query_one(InboxTable)
                    (inbox if inbox.row_count else self.query_one(CandidatesTable)).focus()
            case _:
                assert_never(frame)

    def draw(self, frame: Frame) -> None:
        self.frame = frame
        self.query_one('#header', Static).update(header(frame))
        for table in self.tables():
            table.fill(frame)
        self.show()

    def focused_table(self) -> Pane:
        focused = self.focused
        return focused if isinstance(focused, Pane) else self.query_one(InboxTable)

    def show(self) -> None:
        """Show the selected row of the focused panel in the main pane. While the pane has focus, use the last panel.

        A detail that reads the API reads in a worker. The pane shows it if the cursor is still on that row.
        """
        frame = self.frame
        if frame is None:
            return
        table = self.focused_table() if not isinstance(self.focused, VerticalScroll) else self.shown_table
        self.shown_table = table
        self.shown = Shown(table.id, table.selected(), frame)
        self.query_one('#main', VerticalScroll).border_title = escape(f'[0] {table.id}')
        wanted = self.shown

        def failed(_: Failure) -> None:
            self.undetailed = wanted

        read(self, lambda: table.detail(self.api, frame), lambda d: self.detailed(wanted, d), failed, group='detail')

    shown_table: Pane
    undetailed: Shown | None = None
    """The row whose detail failed to read, until a detail answers: the next draw reads it again."""

    def detailed(self, wanted: Shown, detail: RenderableType) -> None:
        self.undetailed = None
        if wanted == self.shown:
            self.query_one('#detail', Static).update(detail)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = event.data_table
        if (
            table is self.focused
            and isinstance(table, Pane)
            and (table.id, table.selected()) != (self.shown.table, self.shown.key)
        ):
            self.show()

    def on_descendant_focus(self) -> None:
        if isinstance(self.focused, Pane):
            self.show()

    def act(self, write: Callable[[], str]) -> None:
        """Make one write, and say what it did or why the API refused it. The feed brings its effects."""
        try:
            note = write()
        except Refused as e:  # the row changed since the key was pressed, or the caller may not
            note = str(e)
        self.say(note)

    def say(self, note: str) -> None:
        self.note = note
        self.notify(note, timeout=self.main_app.settings.notice.total_seconds())

    def action_panel(self, name: str) -> None:
        self.query_one(f'#{name}', Pane).focus()

    def action_main(self) -> None:
        self.query_one('#main', VerticalScroll).focus()

    def action_filter(self) -> None:
        box = self.query_one(FilterInput)
        self.filtering = self.focused_table()
        box.value = self.filtering.query_text
        box.add_class('shown')
        box.focus()

    filtering: Pane

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.remove_class('shown')
        self.filtering.query_text = event.value.strip()
        self.filtering.focus()
        if self.frame is not None:
            self.draw(self.frame)

    def action_campaigns(self) -> None:
        self.app.pop_screen()


class Watch(App[None]):
    """The app: the campaigns screen at the bottom of the stack, and a campaign screen on top when one is open.

    It owns the ``Follower``, and hands each batch to the screen on top.
    """

    CSS = CSS
    TITLE = 'piloop watch'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('q', 'quit', 'quit'),
        Binding('question_mark', 'keys', 'keys'),
    ]

    def __init__(self, api: Api, ref: str | None, actor: str, settings: WatchSettings) -> None:
        super().__init__()
        self.api, self.ref, self.actor, self.settings = api, ref, actor, settings
        self.latest: dict[CampaignId, Envelope] = {}
        """The last event of each campaign that the feed brought, for the campaigns screen."""
        self.follower = Follower(self)
        self.started = False
        """Whether the follower's start answered, and the screens are up."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def on_mount(self) -> None:
        self.follower.start()

    def on_started(self, message: Started) -> None:
        if message.tail is not None:
            self._seen(message.tail)
        self.started = True
        self.push_screen(CampaignsScreen())
        if self.ref is not None:
            self.push_screen(CampaignScreen(self.ref))

    def on_unmount(self) -> None:
        self.follower.stop.set()

    def _seen(self, batch: Batch) -> None:
        for event in batch.events:
            if event.subject.campaign is not None:
                self.latest[event.subject.campaign] = event

    def on_fed(self, message: Fed) -> None:
        """Hand a batch of the feed to the screen on top. Not under a dialog: it reads when the dialog closes."""
        self._seen(message.batch)
        screen = self.screen
        if isinstance(screen, CampaignsScreen | CampaignScreen) and screen.is_mounted:
            screen.fed(message.batch)

    def on_lost(self, message: Lost) -> None:
        match message.failure:
            case Refused() as refused:
                self.notify(
                    f'the event feed: {refused}', severity='error', timeout=self.settings.notice.total_seconds()
                )
            case failure:
                self.notify(
                    f'the event feed: {failure}; reading again soon',
                    severity='warning',
                    timeout=self.settings.notice.total_seconds(),
                )

    def action_keys(self) -> None:
        """Show or hide the keys of the focused panel beside the screen (``?``)."""
        if self.screen.query(HelpPanel):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()


def read[T](
    node: Screen[None],
    call: Callable[[], T],
    then: Callable[[T], None],
    failed: Callable[[Failure], None],
    group: str = 'read',
) -> None:
    """Make ``call`` in a worker thread of ``node``, and hand its answer to ``then`` in ``node``, or say why it failed.

    ``failed`` gets a failure, in ``node``, which says it. A newer read of the same ``group`` replaces one that has not
    answered. ``node`` drops the answer once it is gone.
    """

    def said(failure: Failure) -> None:
        app = node.app
        assert isinstance(app, Watch)
        node.notify(str(failure), severity='error', timeout=app.settings.notice.total_seconds())
        failed(failure)

    def work() -> None:
        try:
            answer = call()
        except FAILURES as e:
            node.call_later(said, e)
            return
        node.call_later(then, answer)

    node.run_worker(work, thread=True, exclusive=True, group=group)


def plain(cells: tuple[Cell, ...]) -> list[Text]:
    """Return cells as ``Text``: a ``DataTable`` reads a ``str`` as markup, and ``[wip]`` in a summary is data."""
    return [Text(c) if isinstance(c, str) else c for c in cells]
