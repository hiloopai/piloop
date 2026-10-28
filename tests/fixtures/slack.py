"""A local server that answers as Slack's incoming webhooks do: no test reaches Slack."""

import base64
import hashlib
import json
import socket
import struct
import threading
import urllib.parse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class FakeSlack:
    """What the server received, and what it answers next: a status, its headers and its body."""

    url: str
    posts: list[dict[str, object]] = field(default_factory=list)
    answers: list[tuple[int, dict[str, str], str]] = field(default_factory=list)


@contextmanager
def fake_slack() -> Generator[FakeSlack]:
    """Serve until the block ends: each post is in ``posts``, and each answer comes from ``answers``, else 200 ok."""
    fake = FakeSlack('')

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            fake.posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            status, headers, body = fake.answers.pop(0) if fake.answers else (200, {}, 'ok')
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    fake.url = f'http://127.0.0.1:{server.server_port}/services/T0/B0/secret'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()
    thread.join()


WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
"""What RFC 6455 appends to a client's key to make the server's accept header."""


@dataclass
class FakeSlackApp:
    """Slack's Web API and its Socket Mode endpoint, on loopback.

    ``calls`` holds each Web API call as ``(method, body, the Authorization header)``. ``answers`` holds what a method
    answers next, else ``ok`` with a ``ts`` for a post. ``frames`` holds what the client sent on the socket, as JSON.
    """

    api: str = ''
    calls: list[tuple[str, dict[str, object], str]] = field(default_factory=list)
    answers: dict[str, list[tuple[int, dict[str, object]]]] = field(default_factory=dict)
    frames: list[dict[str, object]] = field(default_factory=list)
    connections: int = 0
    _socket: socket.socket | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, message: dict[str, object]) -> None:
        """Send ``message`` to the connected client, as one text frame."""
        assert self._socket is not None, 'no client is connected'
        payload = json.dumps(message).encode()
        size = len(payload)
        head = bytes([0x81, size]) if size < 126 else bytes([0x81, 126]) + struct.pack('>H', size)
        with self._lock:
            self._socket.sendall(head + payload)

    def envelope(self, event: dict[str, object], envelope_id: str = 'env-1') -> None:
        """Send ``event`` as Slack's Events API sends one over Socket Mode."""
        self.push({'envelope_id': envelope_id, 'type': 'events_api', 'payload': {'event': event}})


@contextmanager
def fake_slack_app() -> Generator[FakeSlackApp]:
    """Serve the Web API at ``api`` and a Socket Mode endpoint that ``apps.connections.open`` names, until the end."""
    fake = FakeSlackApp()
    listener = socket.create_server(('127.0.0.1', 0))
    server = ThreadingHTTPServer(('127.0.0.1', 0), _api(fake, listener.getsockname()[1]))
    fake.api = f'http://127.0.0.1:{server.server_port}/api/'
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True),
        threading.Thread(target=_accept, args=(fake, listener), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        listener.shutdown(socket.SHUT_RDWR)  # wakes the accept: a close alone does not, on Linux
        listener.close()
        for thread in threads:
            thread.join()


def _api(fake: FakeSlackApp, ws_port: int) -> type[BaseHTTPRequestHandler]:
    class Api(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0))).decode()
            body: dict[str, object]
            if self.headers.get('Content-Type', '').startswith('application/json'):
                body = json.loads(raw or '{}')
            else:  # Slack's SDK posts a form
                body = {key: value for key, value in urllib.parse.parse_qsl(raw)}
            method = self.path.rsplit('/', 1)[-1]
            fake.calls.append((method, body, self.headers.get('Authorization', '')))
            if queued := fake.answers.get(method):
                status, answer = queued.pop(0)
            elif method == 'apps.connections.open':
                status, answer = 200, {'ok': True, 'url': f'ws://127.0.0.1:{ws_port}/link?ticket=t'}
            else:
                status, answer = 200, {'ok': True, 'channel': body.get('channel'), 'ts': f'{len(fake.calls)}.000100'}
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return None

    return Api


def _accept(fake: FakeSlackApp, listener: socket.socket) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:  # the listener closed: the block ended
            return
        threading.Thread(target=_client, args=(fake, conn), daemon=True).start()


def _client(fake: FakeSlackApp, conn: socket.socket) -> None:
    """Shake hands as RFC 6455 says, say hello as Slack does, then keep what the client sends and answer its pings."""
    request = b''
    while b'\r\n\r\n' not in request:
        if not (chunk := conn.recv(1024)):  # the client went away mid-handshake, as one that closes at once does
            conn.close()
            return
        request += chunk
    key = next(
        line.split(':', 1)[1].strip()
        for line in request.decode().split('\r\n')
        if line.lower().startswith('sec-websocket-key:')
    )
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    conn.sendall(
        'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
        f'Sec-WebSocket-Accept: {accept}\r\n\r\n'.encode()
    )
    fake._socket = conn
    fake.connections += 1
    fake.push({'type': 'hello'})
    try:
        while (frame := _frame(conn)) is not None:
            opcode, payload = frame
            if opcode == 0x1:
                fake.frames.append(json.loads(payload))
            elif opcode == 0x9:  # a ping: Slack's SDK reconnects without the pong
                with fake._lock:
                    conn.sendall(bytes([0x8A, len(payload)]) + payload)
            elif opcode == 0x8:
                return
    except OSError:  # the client went away
        return
    finally:
        conn.close()


def _frame(conn: socket.socket) -> tuple[int, bytes] | None:
    """One frame from a client, unmasked: its opcode and payload. None when the client closed."""
    head = _exactly(conn, 2)
    if head is None:
        return None
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack('>H', _exactly(conn, 2) or b'\0\0')[0]
    elif size == 127:
        size = struct.unpack('>Q', _exactly(conn, 8) or bytes(8))[0]
    mask = _exactly(conn, 4) if head[1] & 0x80 else b'\0\0\0\0'
    payload = _exactly(conn, size) if size else b''
    if mask is None or payload is None:
        return None
    return head[0] & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _exactly(conn: socket.socket, size: int) -> bytes | None:
    data = b''
    while len(data) < size:
        if not (chunk := conn.recv(size - len(data))):
            return None
        data += chunk
    return data
