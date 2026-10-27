"""A local server that answers as Slack's incoming webhooks do: no test reaches Slack."""

import json
import threading
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
