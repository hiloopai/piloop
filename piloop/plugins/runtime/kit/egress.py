"""An HTTP proxy that lets a sandboxed agent reach only the hosts of an allowlist.

A sandbox with no route out gets ``HTTPS_PROXY`` and ``HTTP_PROXY`` naming this proxy, which listens where the sandbox can
reach the host. ``CONNECT host:port`` opens a tunnel, and a request in absolute form (``GET http://host:port/…``)
goes to its origin. Either is refused with 403 unless ``host:port`` is allowed (``Allowed``). The proxy reads no TLS: an
agent's API keys stay end to end.
"""

import asyncio
import contextlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

CHUNK = 65536
HTTPS_PORT = 443
HTTP_PORT = 80


@dataclass(frozen=True)
class Allowed:
    """The destinations of an allowlist: ``host:port``, ``host`` (port 443), or ``*.domain`` for its subdomains."""

    entries: frozenset[str]

    @classmethod
    def of(cls, entries: Iterable[str]) -> Allowed:
        return cls(frozenset(e if ':' in e.rsplit(']', 1)[-1] else f'{e}:{HTTPS_PORT}' for e in entries))

    def __contains__(self, destination: tuple[str, int]) -> bool:
        host, port = destination[0].lower(), destination[1]
        for entry in self.entries:
            pattern, _, allowed_port = entry.rpartition(':')
            pattern = pattern.lower()
            if int(allowed_port) != port:
                continue
            if host == pattern or (pattern.startswith('*.') and host.endswith(pattern[1:])):
                return True
        return False


class EgressProxy:
    """The proxy, on its own thread: ``start`` binds it, ``close`` stops it. ``allow`` changes what it admits."""

    def __init__(self, host: str, port: int, allowed: Allowed) -> None:
        self.host, self.port, self.allowed = host, port, allowed
        self._loop = asyncio.new_event_loop()
        self._server: asyncio.Server | None = None
        self._open: set[asyncio.StreamWriter] = set()
        self._thread = threading.Thread(target=self._loop.run_forever, name=f'egress-{host}:{port}', daemon=True)

    def start(self) -> int:
        """Bind and serve; return the port, the bound one when ``port`` is 0.

        Raises:
            OSError: the address cannot be bound.
        """
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(asyncio.start_server(self._serve, self.host, self.port), self._loop)
        self._server = future.result()
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    def allow(self, destinations: Iterable[str]) -> None:
        """Admit ``destinations`` too, from now on."""
        self.allowed = Allowed(self.allowed.entries | Allowed.of(destinations).entries)

    def close(self) -> None:
        """Stop listening, close every connection, and end the thread."""
        asyncio.run_coroutine_threadsafe(self._close(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    async def _close(self) -> None:
        """Stop accepting, close every connection, the clients' and the origins', and wait until each one is."""
        assert self._server is not None, 'start comes first'
        self._server.close()
        self._server.close_clients()
        for writer in list(self._open):
            writer.close()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._open.add(writer)
        try:
            await self._relay(reader, writer)
        finally:
            self._open.discard(writer)
            await _closed(writer)

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b'\r\n\r\n')
        except asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError:
            return  # the client left, or sent a head too long to be a request: there is nobody to answer
        match head.split(b'\r\n', 1)[0].decode('latin-1').split(' '):
            case [method, target, version]:
                pass
            case _:
                await _answer(writer, 'HTTP/1.1 400 Bad Request', 'not an HTTP request line')
                return
        tunnel = method.upper() == 'CONNECT'
        destination = _destination(target, tunnel)
        if destination is None or destination not in self.allowed:
            await _answer(writer, f'{version} 403 Forbidden', f'{target} is not on the allowlist of this sandbox')
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(*destination)
        except OSError as e:
            await _answer(writer, f'{version} 502 Bad Gateway', f'{target}: {e}')
            return
        self._open.add(up_writer)
        try:
            if tunnel:
                writer.write(f'{version} 200 Connection established\r\n\r\n'.encode('latin-1'))
            else:
                up_writer.write(_origin_form(head))
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            self._open.discard(up_writer)
            await _closed(up_writer)


def _destination(target: str, tunnel: bool) -> tuple[str, int] | None:
    """``(host, port)`` of a CONNECT authority, or of an absolute-form URL. None for anything else."""
    if tunnel:
        host, _, port = target.rpartition(':')
        return (host.strip('[]'), int(port)) if host and port.isdigit() else None
    url = urlsplit(target)
    if url.scheme != 'http' or not url.hostname:
        return None
    return url.hostname, url.port or HTTP_PORT


def _origin_form(head: bytes) -> bytes:
    """The request head with its absolute-form target made origin-form, as an origin server expects."""
    line, rest = head.split(b'\r\n', 1)
    method, target, version = line.split(b' ', 2)
    url = urlsplit(target.decode('latin-1'))
    path = (url.path or '/') + (f'?{url.query}' if url.query else '')
    return b' '.join([method, path.encode('latin-1'), version]) + b'\r\n' + rest


async def _answer(writer: asyncio.StreamWriter, status: str, body: str) -> None:
    data = body.encode()
    writer.write(f'{status}\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode('latin-1') + data)
    with contextlib.suppress(ConnectionError):  # the client may have gone: there is nobody left to tell
        await writer.drain()
    writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # either side may close at any time: the other copy then ends on its own read
    with contextlib.suppress(ConnectionError):
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    writer.close()


async def _closed(writer: asyncio.StreamWriter) -> None:
    """Close ``writer`` and wait until its socket is: a loop that stops first leaves the socket open."""
    writer.close()
    with contextlib.suppress(ConnectionError):  # the peer reset it: closed all the same
        await writer.wait_closed()
