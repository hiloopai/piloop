"""The paths of ``tests/models/forward.tla``, replayed against the real forwarder: two hosts whose ``piloop serve`` is a
standby (``Forwarding``) until it takes the term, then a holder behind the real ``TermCheck``, with crashes, graceful
stops and restarts, and a client's request that goes through them.

Each host has its own address on the loopback. A standby reads the record when the replay lets it (``Refresh``): its
``Leader.current`` waits for the model's next view. Only the network between a forwarder and the holder is held: a
forwarded request waits at its send until the model's ``Handle`` at the holder, and the holder's answer waits until the
model's ``Answer``. A crashed holder stops accepting and never answers what it holds, as a machine that died. After each
step, what the holders applied and what the client got are the model's.

The traces cover every kind of transition of ``forward_graph.cfg`` that the replay tells apart (``--by`` below);
``scripts/graph_traces.py`` wrote them from TLC's ``-dump dot,actionlabels``:
``graph_traces.py graph.dot tests/net/traces/forward --by phase,st,at,via,ans --keep phase,ep,view,rec,st,at,via,sent,res,ans,applied,op,arg``.
"""

import asyncio
import json
import queue
import socket
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from piloop.net import forward as forward_module
from piloop.net.forward import Forwarding, TermCheck
from piloop.plugins.leader.contract import Term
from piloop.plugins.network.contract import Listener
from piloop.settings import DaemonSettings

TRACES = Path(__file__).parent / 'traces' / 'forward'
SETTINGS = DaemonSettings(forward_refresh=timedelta(milliseconds=1), forward_connect=timedelta(seconds=5))
WAIT = 10
HOSTS = (1, 2)
REQUEST = 'x-request'
"""The header that names the model's request, which the forwarder passes on."""
UNTIL = datetime(2100, 1, 1, tzinfo=UTC)

type Value = int | str | bool | list[Value] | dict[str, Value]


def _value(itf: object) -> Value:
    match itf:
        case {'#bigint': str(n)}:
            return int(n)
        case dict():
            return {str(k): _value(v) for k, v in itf.items()}
        case list():
            return [_value(v) for v in itf]
        case bool() | str():
            return itf
    raise AssertionError(f'not an ITF value: {itf!r}')


def until(predicate: Callable[[], object], what: str) -> None:
    """Poll ``predicate`` often: a replay takes thousands of steps."""
    deadline = time.monotonic() + WAIT
    while not predicate():
        assert time.monotonic() < deadline, f'timed out waiting for {what}'
        time.sleep(0.001)


@dataclass
class Held:
    """A step that the replay lets go on: in the event loop that waits on it."""

    loop: asyncio.AbstractEventLoop
    event: asyncio.Event

    def go(self) -> None:
        if not self.loop.is_closed():  # else nothing waits on it any more
            self.loop.call_soon_threadsafe(self.event.set)


@dataclass
class World:
    """What the real code did: which holders applied each request, the held steps, the client's answers."""

    applied: dict[int, int] = field(default_factory=dict)
    sends: dict[int, Held] = field(default_factory=dict)
    """Each forwarded request, held at its send."""
    answers: dict[int, Held] = field(default_factory=dict)
    """Each request that a holder applied, held before its answer."""
    got: dict[int, int | str] = field(default_factory=dict)
    """The client's answer to each request: its status, or the error."""
    lock: threading.Lock = field(default_factory=threading.Lock)

    async def hold(self, into: dict[int, Held], r: int) -> None:
        event = asyncio.Event()
        with self.lock:
            into[r] = Held(asyncio.get_running_loop(), event)
        await event.wait()


class Gate(httpx.AsyncBaseTransport):
    """The network between a forwarder and the holder: a request waits at its send until the replay lets it go."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self.world.hold(self.world.sends, int(request.headers[REQUEST]))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class HolderApp:
    """The holder's API: it applies each request, then answers once the replay lets it."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        more = True
        while more:
            more = (await receive()).get('more_body', False)
        r = int(dict(scope['headers'])[REQUEST.encode()])
        with self.world.lock:
            self.world.applied[r] = self.world.applied.get(r, 0) + 1
        await self.world.hold(self.world.answers, r)
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'applied'})


class Holder:
    """A holder on its host's address: uvicorn serving ``HolderApp`` behind ``TermCheck``."""

    def __init__(self, world: World, sock: socket.socket, epoch: int) -> None:
        config = uvicorn.Config(
            TermCheck(HolderApp(world), epoch), lifespan='off', log_config=None, timeout_graceful_shutdown=0
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        self.thread.start()
        until(lambda: self.server.started, 'the holder serves')

    def die(self) -> None:
        """A machine that died: it accepts no more connections, and never answers those that it has."""
        loop = self.server.servers[0].get_loop()
        loop.call_soon_threadsafe(lambda: [each.close() for each in self.server.servers])
        until(lambda: not any(each.is_serving() for each in self.server.servers), 'the holder stops accepting')

    def stop(self) -> None:
        """End it: what it holds is cancelled, and its connections close (``timeout_graceful_shutdown`` 0)."""
        self.server.should_exit = True
        self.thread.join(WAIT)


@dataclass
class Host:
    """One host: its address, and the ``piloop serve`` on it now."""

    port: int
    sock: socket.socket | None = None
    standby: Forwarding | None = None
    views: queue.Queue[Term | None] = field(default_factory=queue.Queue)
    holder: Holder | None = None
    dead: list[Holder] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'


class Replay:
    def __init__(self, world: World) -> None:
        self.world = world
        self.hosts: dict[int, Host] = {}
        for h in HOSTS:
            sock = socket.create_server(('127.0.0.1', 0))
            self.hosts[h] = Host(sock.getsockname()[1], sock)
            self.start_standby(h)
        self.clients: list[threading.Thread] = []

    def start_standby(self, h: int) -> None:
        host = self.hosts[h]
        assert host.sock is not None
        host.views = queue.Queue()
        host.standby = Forwarding(host.views.get, [Listener(host.sock, None)], SETTINGS)
        host.standby.start()

    def term(self, view: Value) -> Term | None:
        assert isinstance(view, dict)
        epoch, holder = view['epoch'], view['holder']
        assert isinstance(epoch, int) and isinstance(holder, int)
        return None if holder == 0 else Term(epoch=epoch, holder=f'h{holder}', url=self.hosts[holder].url, until=UNTIL)

    def epoch_read(self, h: int) -> object:
        return httpx.get(f'{self.hosts[h].url}/v1/ready', timeout=WAIT).json()['epoch']

    def request(self, r: int, h: int) -> None:
        def call() -> None:
            try:
                answer = httpx.post(f'{self.hosts[h].url}/v1/x', content=b'{}', headers={REQUEST: str(r)}, timeout=60)
            except httpx.HTTPError as e:
                self.world.got[r] = type(e).__name__
            else:
                self.world.got[r] = answer.status_code

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        self.clients.append(thread)

    def step(self, op: str, arg: int, before: dict[str, Value], after: dict[str, Value]) -> None:
        r = arg
        match op:
            case 'take':
                standby = self.hosts[arg].standby
                assert standby is not None
                standby.hold(_int(after['ep'], arg))
                self.hosts[arg].views.put(None)  # the read that waits ends, and lands nowhere
                until(lambda: self.epoch_read(arg) == _int(after['ep'], arg), 'it starts its term')
            case 'serve':
                host = self.hosts[arg]
                assert host.standby is not None and host.sock is not None
                host.standby.close()
                host.standby, host.holder = None, Holder(self.world, host.sock, _int(after['ep'], arg))
            case 'refresh':
                view = _at(after['view'], arg)
                self.hosts[arg].views.put(self.term(view))
                assert isinstance(view, dict)
                until(lambda: self.epoch_read(arg) == (view['epoch'] or None), 'the standby reads the new term')
            case 'crash' | 'stop':
                host = self.hosts[arg]
                if host.standby is not None:
                    host.standby.close()
                    host.views.put(None)
                    assert host.sock is not None
                    host.sock.close()
                elif op == 'crash':
                    assert host.holder is not None
                    host.holder.die()
                    host.dead.append(host.holder)
                else:
                    assert host.holder is not None
                    host.holder.stop()
                host.standby = host.holder = host.sock = None
            case 'restart':
                host = self.hosts[arg]
                host.sock = socket.create_server(('127.0.0.1', host.port))
                self.start_standby(arg)
            case 'handle':
                if _int(before['via'], r) == 0:  # the client sends it
                    self.request(r, _int(before['at'], r))
                else:  # the forwarded request reaches the holder
                    self.world.sends.pop(r).go()
            case 'answer':
                self.world.answers.pop(r).go()
            case (
                'send' | 'expire' | 'relay' | 'done'
            ):  # nothing that the code does: the client chose a host; time passed
                pass
            case _:
                raise AssertionError(f'a step that the replay does not know: {op}')
        self.settle(after)

    def settle(self, state: dict[str, Value]) -> None:
        """Wait until the real code is where the model is: each request held, applied or answered as it says."""
        r = 1
        st, via, res, ans, applied = (_at(state[k], r) for k in ('st', 'via', 'res', 'ans', 'applied'))
        # an answer on its way back to the forwarder goes on at once: the client has it before the model's ``Relay``
        answered = res if st == 'reply' else ans if st == 'done' else None
        if st == 'in' and via != 0:
            until(lambda: r in self.world.sends, 'the forwarder sends it')
        elif st == 'app':
            until(lambda: r in self.world.answers, 'the holder holds its answer')
        elif answered is not None and answered != 'reset':  # a reset is a process that the replay did not kill
            until(lambda: r in self.world.got, 'the client gets an answer')
            expected = 200 if answered == 'ok' else 503
            assert self.world.got[r] == expected, f'the client got {self.world.got[r]}, the model {answered}'
        assert self.world.applied.get(r, 0) == applied, 'the holders applied it as often as the model'

    def close(self) -> None:
        with self.world.lock:  # first, while their loops run: the clients that wait on them get their answers
            held = [*self.world.sends.values(), *self.world.answers.values()]
        for each in held:
            each.go()
        for host in self.hosts.values():
            if host.standby is not None:
                host.standby.close()
                host.views.put(None)
            for holder in [*host.dead, *([] if host.holder is None else [host.holder])]:
                holder.stop()
            if host.sock is not None:
                host.sock.close()
        for thread in self.clients:
            thread.join(WAIT)


def _at(values: Value, i: int) -> Value:
    """The model's value of host or request ``i``: TLC writes a function over ``1..N`` as a sequence."""
    assert isinstance(values, list)
    return values[i - 1]


def _int(values: Value, i: int) -> int:
    found = _at(values, i)
    assert isinstance(found, int)
    return found


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch) -> Generator[World]:
    world = World()
    real = httpx.AsyncClient
    monkeypatch.setattr(forward_module.httpx, 'AsyncClient', lambda **kw: real(transport=Gate(world), **kw))
    yield world


def traces() -> list[Path]:
    return sorted(TRACES.glob('trace_*.itf.json'), key=lambda p: int(p.stem.split('_')[1].split('.')[0]))


def test_the_traces_cover_the_model() -> None:
    assert len(traces()) == 187


@pytest.mark.parametrize('trace', traces(), ids=lambda p: p.stem.split('.')[0])
def test_a_path_of_the_model(trace: Path, gated: World) -> None:
    states = [_value(s['s']) for s in json.loads(trace.read_text())['states']]
    replay = Replay(gated)
    try:
        for before, after in zip(states, states[1:], strict=False):
            assert isinstance(before, dict) and isinstance(after, dict)
            op, arg = after['op'], after['arg']
            assert isinstance(op, str) and isinstance(arg, int)
            replay.step(op, arg, before, after)
    finally:
        replay.close()
