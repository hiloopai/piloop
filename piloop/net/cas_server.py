"""``piloop cas serve``: a home's CAS over HTTP, for workers on other machines, in a process of its own.

It is not ``piloop serve``: the control plane passes only CIDs, and workers read and write blobs here directly. It
serves the wire format of ``piloop.net.cas``. A caller needs a worker's token (``piloop token create``).

Each body passes through a temporary file: an upload is hashed there before the CAS stores it, and a download is
checked there before its first byte is sent, so a corrupt blob never goes out.

Retirement: deleted, with ``piloop/cli/cas.py``, when covalence's HTTP CAS server ships with a token hook and a
directory backend, and piloop serves that instead.
"""

import sys
import tempfile
import threading
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from covalence import Cid, StoreError
from fastapi import Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.types import Receive, Scope, Send

from piloop.api.contract import Access
from piloop.daemon.serve import running_pid
from piloop.db.session import Database, schema_mismatch
from piloop.net.cas import ROUTE, Code, ErrorBody
from piloop.net.forward import Forwarding
from piloop.net.server import App, Server, authorized, listening
from piloop.net.tokens import Tokens
from piloop.plugins.cas.contract import CAS, UNBOUNDED, BlobAddress, Cas
from piloop.plugins.leader.contract import Leader, Term, Unreachable
from piloop.plugins.network.contract import Listener, Network, NetworkConfig, NetworkHost
from piloop.plugins.secrets.contract import SECRETS, HomeSecrets
from piloop.settings import Settings

ADDRESS = TypeAdapter(BlobAddress)
CHUNK = 1 << 20
TMP = 'tmp'
"""``<PILOOP_HOME>/tmp``: where a body waits until it is checked."""
STATUS = {
    Code.UNAUTHENTICATED: 401,
    Code.PERMISSION_DENIED: 403,
    Code.INVALID_CID: 400,
    Code.LENGTH_REQUIRED: 411,
    Code.CID_MISMATCH: 400,
    Code.NOT_FOUND: 404,
    Code.INVALID_REQUEST: 400,
    Code.UNAVAILABLE: 503,
    Code.INTERNAL: 500,
}
REFUSED = {401: Code.UNAUTHENTICATED, 403: Code.PERMISSION_DENIED, 404: Code.NOT_FOUND}
"""The code of an ``HTTPException`` by its status, from the token check or the router; any other is ``invalid_request``."""
RETRY_AFTER = '1'


class CasError(Exception):
    """A request that the service answers with an error: its ``code`` and why."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code


def app(tokens: Tokens, network: Network, cas: Cas, scratch: Path) -> App:
    """The routes of ``ROUTE`` over ``cas``, with temporary files in ``scratch``, for workers."""
    workers = [Depends(authorized(Access.WORKER))]
    server = App(tokens, network)

    @server.exception_handler(CasError)
    def refused(request: Request, e: CasError) -> Response:
        return _answer(STATUS[e.code], e.code, str(e))

    @server.exception_handler(HTTPException)
    def http(request: Request, e: HTTPException) -> Response:
        return _answer(e.status_code, REFUSED.get(e.status_code, Code.INVALID_REQUEST), e.detail, e.headers)

    @server.exception_handler(StoreError)
    def unavailable(request: Request, e: StoreError) -> Response:
        message = 'the store is briefly unavailable; retry shortly'
        return _answer(503, Code.UNAVAILABLE, message, {'Retry-After': RETRY_AFTER})

    @server.exception_handler(Exception)
    def internal(request: Request, e: Exception) -> Response:
        return _answer(500, Code.INTERNAL, 'internal error')

    @server.head(ROUTE, dependencies=workers)
    def head(cid: str) -> Response:
        if not cas.has(_address(cid)):
            raise CasError(Code.NOT_FOUND, f'no blob {cid}')
        found = Response()
        del found.headers['content-length']  # not 0: the contract has no size
        return found

    @server.get(ROUTE, dependencies=workers)
    def get(cid: str) -> Response:
        address = _address(cid)
        scratch.mkdir(parents=True, exist_ok=True)
        with ExitStack() as owned:
            spool = owned.enter_context(tempfile.TemporaryFile(dir=scratch))
            size = cas.get_to(address, spool, max_bytes=UNBOUNDED)
            if size is None:
                raise CasError(Code.NOT_FOUND, f'no blob {cid}')
            owned.pop_all()  # the response closes the spool
        return _Spooled(spool, size)

    @server.put(ROUTE, dependencies=workers, status_code=201)
    async def put(cid: str, request: Request) -> Response:
        address = _address(cid)
        if 'content-length' not in request.headers:
            raise CasError(Code.LENGTH_REQUIRED, 'Content-Length is required')
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=scratch) as spool:
            async for chunk in request.stream():
                spool.write(chunk)
            await run_in_threadpool(_store, cas, address, spool)
        return Response(status_code=201)

    return server


def _address(text: str) -> Cid:
    try:
        return ADDRESS.validate_python(text)
    except ValidationError as e:
        raise CasError(Code.INVALID_CID, f'{text!r} is no BLAKE3 CID (bafkr4…)') from e


def _store(cas: Cas, cid: Cid, spool: BinaryIO) -> None:
    """Store the bytes of ``spool`` once they hash to ``cid``."""
    spool.seek(0)
    if Cid.of_fileobj(spool, max_bytes=UNBOUNDED).digest != cid.digest:
        raise CasError(Code.CID_MISMATCH, f'the bytes do not hash to {cid}; nothing was stored')
    cas.put_fileobj(spool, max_bytes=UNBOUNDED)


class _Spooled(StreamingResponse):
    """The bytes of ``spool``, a temporary file that the response closes once it ends, also when the client left."""

    def __init__(self, spool: BinaryIO, size: int) -> None:
        spool.seek(0)
        super().__init__(
            iter(partial(spool.read, CHUNK), b''),
            media_type='application/octet-stream',
            headers={'Content-Length': str(size)},
        )
        self.spool = spool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.spool.close()


def _answer(status: int, code: Code, message: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(ErrorBody(code=code, message=message).model_dump(), status, headers)


def bind(cfg: Settings, network: NetworkConfig) -> Listener:
    """Bind ``network``'s listener, before anything opens the database: a standby has none.

    Raises:
        OSError: It could not bind.
    """
    unopened = Database(cfg.database_url, actor=cfg.actor)  # a network's open keeps it, and binding reads nothing
    try:
        listener = listening(
            network.open(NetworkHost(Tokens(unopened), HomeSecrets(unopened, SECRETS.selected(cfg.providers))))
        )
    finally:
        unopened.close()
    assert listener is not None  # the network listens: `listening` set its address
    return listener


def serve(cfg: Settings, network: NetworkConfig, listener: Listener, leader: Leader, stop: threading.Event) -> None:
    """Serve the CAS on ``listener`` until ``stop``, switching as the home's term moves (``Leader.here``).

    While the daemon of this host holds the term, it answers from this host's database. Otherwise it forwards each
    request to the holder's CAS server, or answers 503 while none holds it (``piloop.net.forward``): a standby has no
    database, or a stale one, so it checks no token itself, and it never exits for want of one.
    """
    here = _holds(leader, here=False)
    every = cfg.daemon.forward_refresh.total_seconds()
    while not stop.is_set():
        with (_here if here else _forward)(cfg, network, listener, leader):
            while not stop.wait(every) and _holds(leader, here=here) == here:
                pass
        here = not here


def _holds(leader: Leader, *, here: bool) -> bool:
    """Whether the daemon of this host holds the home's term; ``here``, as it was, while the record is unreadable."""
    try:
        return leader.here()
    except Unreachable as e:
        print(f'piloop: the CAS server could not read the term: {e}', file=sys.stderr)
        return here


@contextmanager
def _here(cfg: Settings, network: NetworkConfig, listener: Listener, leader: Leader) -> Generator[None]:
    """Serve from this host's database, which the daemon here holds the term of, in a thread."""
    if (e := schema_mismatch(cfg.database, running_pid(cfg), cfg.actor)) is not None:
        raise SystemExit(str(e)) from e
    db = Database(cfg.database_url, actor=cfg.actor)
    try:
        tokens = Tokens(db, cfg.daemon.token_trust.total_seconds())
        net = network.open(NetworkHost(tokens, HomeSecrets(db, SECRETS.selected(cfg.providers))))
        cas = CAS.selected(cfg.providers).open(cfg.home)
        server = Server(app(tokens, net, cas, cfg.home / TMP), Listener(listener.socket.dup(), listener.tls))
        server.start()  # it closes its duplicate of the socket as it ends; the next server takes the socket
        try:
            yield
        finally:
            server.close()
    finally:
        db.close()


@contextmanager
def _forward(cfg: Settings, network: NetworkConfig, listener: Listener, leader: Leader) -> Generator[None]:
    """Forward each request to the CAS server of the term's holder, in a thread."""
    port = listener.socket.getsockname()[1]
    forwarding = Forwarding(lambda: _cas_of(leader.current(), port), [listener], cfg.daemon)
    forwarding.start()
    try:
        yield
    finally:
        forwarding.close()


def _cas_of(term: Term | None, port: int) -> Term | None:
    """``term``, with the URL of its holder's CAS server: the host of the term's URL, at ``port``.

    The CAS servers of a home's hosts listen on one port, as a StatefulSet's pods do.
    """
    if term is None or term.url is None:
        return term
    parts = urlsplit(term.url)
    host = parts.hostname or ''
    netloc = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    return term.model_copy(update={'url': parts._replace(netloc=netloc).geturl()})
