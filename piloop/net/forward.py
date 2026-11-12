"""A standby's server: a ``piloop serve`` that does not hold the home's term forwards each request to the daemon that does.

The daemon binds its sockets before it takes the term (``piloop.daemon.main``), and serves them with ``Forwarding``
while it waits. Every ``forward_refresh`` it reads which daemon holds the term (``Leader.current``), and sends each
request there, at the term's URL, as it came: its method, path, headers (the caller's own token too: the holder checks
it as always) and body, streamed both ways. It adds ``Piloop-Term``, the epoch that it forwards to.

- **One hop.** A standby answers a request that names a term 409 ``term`` (``refusal``), and never forwards it: a
  request that a stale standby sent to another standby goes no further. The holder ignores the header: whatever term a
  standby read, the daemon that serves is the one that holds the term: TLC needs no check there (``forward.tla``).
- **503, Retry-After.** No term, a holder that does not answer, or a 409 ``term``: the client sends it again later.
  A standby never sends a request twice: the holder may have applied it.
- **Long-polls and streams.** Once connected, a forwarded request waits as long as the holder takes. It ends when the
  client goes, or when the term it went to ends: a forwarder that reads a new term answers 503 to what it sent to the
  old one (or cuts a stream that started). A holder on a machine that died sends no reset, and this bounds the wait.
- **The home's Unix socket** answers 421 ``standby``: its caller is the home's owner by being on this host, an identity
  that the holder cannot check (no stamped identity), so the standby does not forward it.
- **Taking the term.** From the take on (``hold``), the daemon answers 503: it restores, migrates and rebuilds the home.
  Then ``close`` stops these servers, and the daemon's own serve on the same sockets.

``GET /v1/ready`` answers here, with no token: 200 while a standby knows a holder to forward to. The model is
``tests/models/forward.tla``.
"""

import asyncio
import json
import math
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from http import HTTPStatus

import httpx2 as httpx
import uvicorn
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from piloop.api.contract import READY, Problem
from piloop.api.views import Readiness, Role
from piloop.net.server import on_socket, uvicorn_config
from piloop.plugins.leader.contract import Term, Unreachable
from piloop.plugins.network.contract import Listener
from piloop.settings import DaemonSettings

TERM_HEADER = 'piloop-term'
"""The epoch that a standby forwarded a request to: another standby refuses it (``refusal``)."""
HOP_BY_HOP = frozenset(
    {
        b'connection',
        b'keep-alive',
        b'proxy-authenticate',
        b'proxy-authorization',
        b'te',
        b'trailer',
        b'transfer-encoding',
        b'upgrade',
        b'host',
    }
)
"""The headers of one connection, which a proxy does not pass on (RFC 9110 §7.6.1), and ``Host``, which names it."""
OWN = frozenset({b'date', b'server'})
"""The headers of an answer that the standby's own server adds."""
START_SECONDS = 10
"""How long ``Forwarding.start`` waits for its servers to accept."""


def refusal(headers: Headers) -> Response | None:
    """A standby's 409 ``term`` to a request that another standby forwarded: it goes no further."""
    named = headers.get(TERM_HEADER)
    if named is None:
        return None
    return JSONResponse(
        {
            'error': Problem.TERM,
            'detail': f'a standby forwarded this request for term {named} to this daemon, a standby',
        },
        status_code=HTTPStatus.CONFLICT,
    )


class _Gone(Exception):
    """The client went away before it sent its whole request."""


class _Stale(Exception):
    """The term's URL led to a standby, which answered 409 ``term``: the term read last is over."""


class Forwarding:
    """The standby's server on ``listeners``, from ``start`` until ``close``: each request goes on to the holder.

    Its servers run in one thread, on one event loop. They serve duplicates of the sockets, so that ``close`` leaves
    the sockets bound for the daemon's own servers. ``close`` may come more than once, and before ``start``.
    """

    def __init__(
        self, current: Callable[[], Term | None], listeners: Sequence[Listener], settings: DaemonSettings
    ) -> None:
        """``current``: the term that a daemon holds now (``Leader.current``)."""
        self._current = current
        self._every = settings.forward_refresh.total_seconds()
        self._retry = str(math.ceil(self._every))
        self._connect = settings.forward_connect.total_seconds()
        self._servers = [(uvicorn.Server(uvicorn_config(self, each)), each) for each in listeners]
        self._thread = threading.Thread(target=self._run, name='piloop-forward', daemon=True)
        self._failed: Exception | None = None
        """Why the thread ended before ``close``."""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._term: Term | None = None
        """The term that a daemon holds now, as the latest read found it."""
        self._held: int | None = None
        """The term that this daemon took (``hold``): it answers 503 from then on."""
        self._over = asyncio.Event()
        """Set once the term of ``_term`` ends for this forwarder: what each request sent there waits on besides."""
        self._client: httpx.AsyncClient | None = None
        self._reading = threading.Event()
        """Set while the reads of the term go on: until this daemon takes one, or the forwarding closes."""
        self._posting = threading.Lock()
        """Held to post a read into the loop, and to end the reads: no read lands in a loop that closed."""

    def start(self) -> None:
        """Serve in a thread, and return once every server accepts.

        Raises:
            RuntimeError: A server did not start within ``START_SECONDS``.
        """
        self._thread.start()
        deadline = time.monotonic() + START_SECONDS
        while not all(server.started for server, _ in self._servers):
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('the standby did not serve its sockets') from self._failed
            time.sleep(0.01)

    def hold(self, epoch: int) -> None:
        """This daemon took term ``epoch``: answer 503 from now on, and to each request in flight to the term before.

        It returns once the servers answer so: a probe never finds a standby in a daemon that took the term.
        """
        assert self._loop is not None  # ``start`` waited for it
        self._end_reads()
        asyncio.run_coroutine_threadsafe(self._hold(epoch), self._loop).result()

    def close(self) -> None:
        """Stop serving, and wait until the servers closed their duplicates of the sockets.

        A request in flight answers 503 at once: the sockets stay bound, and the daemon's own servers answer from there.
        A read of the term under way is not waited for: it ends on its own, and lands nowhere.
        """
        self._end_reads()
        if self._loop is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop)
        if self._thread.ident is not None:
            self._thread.join()

    def _end_reads(self) -> None:
        with self._posting:
            self._reading.clear()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as e:  # ``start`` raises it
            self._failed = e

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._over = asyncio.Event()  # of this loop
        timeout = httpx.Timeout(None, connect=self._connect)
        limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            self._client = client
            self._reading.set()
            threading.Thread(target=self._read, args=(self._loop,), name='piloop-forward-reads', daemon=True).start()
            # duplicates: a server closes its socket as it ends, and the daemon's own serve on the same ones next
            dups = [each.socket.dup() for _, each in self._servers]
            try:
                await asyncio.gather(
                    *(server.serve(sockets=[dup]) for (server, _), dup in zip(self._servers, dups, strict=True))
                )
            finally:
                self._end_reads()  # before the loop closes, however it ends: no read lands in a closed loop
                for dup in dups:
                    dup.close()

    def _stop(self) -> None:
        self._over.set()
        for server, _ in self._servers:
            server.should_exit = True

    async def _hold(self, epoch: int) -> None:
        self._held = epoch
        self._over.set()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        """Read the term that a daemon holds now, every ``forward_refresh``, until the reads end.

        A thread of its own, which nothing joins: a read that hangs holds up no stop.
        """
        while self._reading.is_set():
            try:
                term = self._current()
            except Unreachable as e:  # the term read last stands: a stale one ends in 503s, never in a wrong write
                print(f'piloop: the standby could not read the term: {e}', file=sys.stderr)
            else:
                with self._posting:
                    if self._reading.is_set():
                        loop.call_soon_threadsafe(self._seen, term)
            time.sleep(self._every)

    def _seen(self, term: Term | None) -> None:
        """``term`` is the one that a daemon holds now: a new one ends what went to the one before."""
        if term is None or self._term is None or term.epoch != self._term.epoch:
            self._over.set()
            self._over = asyncio.Event()
        self._term = term

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope['type'] == 'http'  # lifespan is off, and the API takes no websocket
        term = self._term
        if scope['path'] == READY:
            role = Role.STANDBY if self._held is None else Role.STARTING
            epoch = self._held or (None if term is None else term.epoch)
            serves = self._held is None and term is not None and term.url is not None
            readiness = Readiness(serves=serves, role=role, epoch=epoch).model_dump()
            await self._answer(readiness, HTTPStatus.OK if serves else None)(scope, receive, send)
            return
        if (refused := refusal(Headers(scope=scope))) is not None:
            await refused(scope, receive, send)
            return
        if self._held is not None:
            await self._unavailable(f'this daemon took term {self._held}, and starts')(scope, receive, send)
            return
        if term is None:
            await self._unavailable("no daemon holds the home's term now")(scope, receive, send)
            return
        if on_socket(scope):
            await JSONResponse(
                {
                    'error': Problem.STANDBY,
                    'detail': f'this daemon is a standby: term {term.epoch} is held by {term.holder}'
                    f' ({term.url or "serving no other machine"}). Run the command on its host, or through the'
                    ' server with a token of the home',
                },
                status_code=HTTPStatus.MISDIRECTED_REQUEST,
            )(scope, receive, send)
            return
        if term.url is None:
            await self._unavailable(f'the holder of term {term.epoch}, {term.holder}, serves no other machine')(
                scope, receive, send
            )
            return
        await self._forward(scope, receive, send, term, self._over)

    async def _forward(self, scope: Scope, receive: Receive, send: Send, term: Term, over: asyncio.Event) -> None:
        """Send the request to the holder of ``term``, once, and pass its answer back as it streams.

        It ends when the answer did, when the client goes, or when ``over`` is set: the term ended for this forwarder.
        """
        assert self._client is not None
        sent = asyncio.Event()
        began = asyncio.Event()
        """Set once the holder's answer began to go back: from then on, a failure can only cut it."""
        relaying = asyncio.ensure_future(
            _relay(self._client, _request(self._client, scope, receive, term, sent), send, began)
        )
        ended = asyncio.ensure_future(over.wait())
        gone = asyncio.ensure_future(_gone(receive, sent))
        try:
            await asyncio.wait({relaying, ended, gone}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for each in (relaying, ended, gone):
                each.cancel()
            await asyncio.gather(relaying, ended, gone, return_exceptions=True)  # the relay's response closes first
        if began.is_set() or not gone.cancelled():
            return  # answered, or a stream cut that its client sees end; or the client went
        match None if relaying.cancelled() else relaying.exception():
            case _Gone():
                return
            case _Stale():
                why = f'{term.url} is a standby now: term {term.epoch} is over'
            case httpx.TransportError() as e:
                why = f'the holder of term {term.epoch} ({term.url}) did not answer ({type(e).__name__})'
            case None:  # the term ended first
                why = f'term {term.epoch} ended while its holder had the request'
            case failed:
                raise failed
        await self._unavailable(why)(scope, receive, send)

    def _unavailable(self, why: str) -> Response:
        return self._answer({'error': Problem.UNAVAILABLE, 'detail': f'{why}: retry in {self._retry} s'}, None)

    def _answer(self, body: dict[str, object], status: HTTPStatus | None) -> Response:
        """``body``, with ``status``; None: 503 with a Retry-After of ``forward_refresh``."""
        if status is not None:
            return JSONResponse(body, status_code=status)
        return JSONResponse(body, status_code=HTTPStatus.SERVICE_UNAVAILABLE, headers={'Retry-After': self._retry})


def _request(
    client: httpx.AsyncClient, scope: Scope, receive: Receive, term: Term, sent: asyncio.Event
) -> httpx.Request:
    """The request of ``scope``, to the holder of ``term``, with its body as the client sends it.

    ``sent`` is set once the whole body went: from then on, a ``receive`` is the client's going away.
    """
    assert term.url is not None

    async def body() -> AsyncIterator[bytes]:
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                raise _Gone
            yield message.get('body', b'')
            if not message.get('more_body', False):
                break
        sent.set()

    headers = Headers(scope=scope)
    streams = 'content-length' in headers or 'transfer-encoding' in headers
    if not streams:
        sent.set()
    query = scope['query_string']
    target = f'{term.url.rstrip("/")}{scope["raw_path"].decode()}{"?" + query.decode() if query else ""}'
    passed = [(k, v) for k, v in scope['headers'] if k.lower() not in HOP_BY_HOP]
    return client.build_request(
        scope['method'],
        target,
        headers=[*passed, (TERM_HEADER.encode(), str(term.epoch).encode())],
        content=body() if streams else None,
    )


async def _relay(client: httpx.AsyncClient, request: httpx.Request, send: Send, began: asyncio.Event) -> None:
    """Send ``request``, and pass the holder's answer back as it streams; ``began`` is set once it began to go back.

    Raises:
        _Stale: A standby answered 409 ``term``.
        httpx.TransportError: The holder did not answer.
    """
    response = await client.send(request, stream=True)
    try:
        chunks = response.aiter_raw()
        if response.status_code == HTTPStatus.CONFLICT:  # small: read whole, to tell a refusal of the term
            raw = b''.join([chunk async for chunk in chunks])
            if _stale(raw):
                raise _Stale
            chunks = _once(raw)
        kept = [(k, v) for k, v in response.headers.raw if k.lower() not in HOP_BY_HOP | OWN]
        await send({'type': 'http.response.start', 'status': response.status_code, 'headers': kept})
        began.set()
        async for chunk in chunks:
            await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
    finally:
        await response.aclose()


async def _gone(receive: Receive, sent: asyncio.Event) -> None:
    """Return once the client went away, after its whole request was read."""
    await sent.wait()
    message: Message = {'type': ''}
    while message['type'] != 'http.disconnect':
        message = await receive()


async def _once(body: bytes) -> AsyncIterator[bytes]:
    yield body


def _stale(body: bytes) -> bool:
    """Whether a 409's body is ``refusal``'s."""
    try:
        return json.loads(body).get('error') == Problem.TERM
    except ValueError, AttributeError:
        return False
