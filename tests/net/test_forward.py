"""A standby's server (``piloop.net.forward``): each request goes on to the holder of the term, once, with its epoch."""

import asyncio
import json
import socket
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2 as httpx
import pytest
import uvicorn
from starlette.types import Receive, Scope, Send

from piloop.api.contract import Problem
from piloop.net import forward as forward_module
from piloop.net.forward import Forwarding, _stale
from piloop.net.server import advertised, local_listener
from piloop.plugins.leader.contract import Term, Unreachable
from piloop.plugins.network.contract import Listener
from piloop.settings import DaemonSettings, socket_at
from tests.conftest import wait_until

FAST = DaemonSettings(forward_refresh=timedelta(seconds=0.01), forward_connect=timedelta(seconds=2))
WAIT = 10


class Record:
    """The term record, as ``Leader.current`` reads it: what the test sets, or the error it raises."""

    def __init__(self) -> None:
        self.term: Term | None = None
        self.error: Unreachable | None = None
        self.reads = 0

    def current(self) -> Term | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.term

    def holds(self, epoch: int, url: str | None) -> None:
        self.term = Term(epoch=epoch, holder=f'host-{epoch}/1', url=url, until=datetime(2100, 1, 1, tzinfo=UTC))


class Holder:
    """The routes of a holder: what it got, and a long-poll that ends when told."""

    def __init__(self) -> None:
        self.got: list[dict[str, object]] = []
        self.gone = threading.Event()
        """Set once a client of ``/hold`` went away."""
        self.go_on = threading.Event()
        """Lets ``/stream`` send its second chunk."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = b''
        more = True
        while more:
            message = await receive()
            body += message.get('body', b'')
            more = message.get('more_body', False)
        headers = {k.decode(): v.decode() for k, v in scope['headers']}
        self.got.append(
            {'path': scope['path'], 'query': scope['query_string'].decode(), 'body': body.decode(), **headers}
        )
        match scope['path'].rsplit('/', 1)[-1]:
            case 'hold':
                while (await receive())['type'] != 'http.disconnect':
                    pass
                self.gone.set()
            case 'stream':
                start = {'type': 'http.response.start', 'status': 200, 'headers': [(b'x-from', b'holder')]}
                await send(start)
                await send({'type': 'http.response.body', 'body': b'one ', 'more_body': True})
                await asyncio.to_thread(self.go_on.wait, WAIT)
                await send({'type': 'http.response.body', 'body': b'two', 'more_body': False})
            case 'refused':
                await _json(send, 409, b'{"error": "refused", "detail": "as the test says"}')
            case 'garbled':
                await _json(send, 409, b'not json')
            case _:
                await _json(send, 201, json.dumps(self.got[-1]).encode())


async def _json(send: Send, status: int, body: bytes) -> None:
    await send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json')]})
    await send({'type': 'http.response.body', 'body': body})


@contextmanager
def serving(app: Callable[[Scope, Receive, Send], object]) -> Generator[str]:
    """``app`` on uvicorn, on a port of the loopback, for the block: its URL."""
    sock = socket.create_server(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan='off', log_config=None, timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    wait_until(lambda: server.started, 'the holder serves')
    try:
        yield f'http://127.0.0.1:{sock.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join()
        sock.close()


@pytest.fixture
def record() -> Record:
    return Record()


@pytest.fixture
def standby(record: Record, tmp_path: Path) -> Generator[tuple[Forwarding, str, Path]]:
    """A standby that forwards on a port of the loopback and on a Unix socket: its forwarding, URL and socket."""
    path = socket_at(tmp_path / 'piloop.sock')  # macOS's temporary directory is too deep for a socket's path
    local = local_listener(path)
    remote = Listener(socket.create_server(('127.0.0.1', 0)), None)
    forwarding = Forwarding(record.current, [local, remote], FAST)
    forwarding.start()
    try:
        yield forwarding, f'http://127.0.0.1:{remote.socket.getsockname()[1]}', path
    finally:
        forwarding.close()
        forwarding.close()  # a second close does nothing
        local.socket.close()
        remote.socket.close()


def ready(url: str) -> httpx.Response:
    return httpx.get(f'{url}/v1/ready', timeout=WAIT)


def forwards_to(url: str, epoch: int | None) -> None:
    wait_until(lambda: ready(url).json()['epoch'] == epoch, f'the standby reads term {epoch}')


def test_a_request_goes_to_the_holder_once_as_it_came_with_its_term_and_its_answer_streams_back(
    standby: tuple[Forwarding, str, Path], record: Record
) -> None:
    holder = Holder()
    _, url, _ = standby
    with serving(holder) as at:
        record.holds(4, at + '/')
        forwards_to(url, 4)
        assert ready(url).status_code == 200 and ready(url).json() == {'serves': True, 'role': 'standby', 'epoch': 4}
        answer = httpx.post(
            f'{url}/v1/echo?x=1', content=b'{"a": 1}', headers={'authorization': 'Bearer plt_x', 'te': 'trailers'}
        )
        assert answer.status_code == 201
        [got] = holder.got
        assert (got['path'], got['query'], got['body'], got['authorization']) == (
            '/v1/echo',
            'x=1',
            '{"a": 1}',
            'Bearer plt_x',
        )
        assert got['piloop-term'] == '4', 'the epoch that it forwards to'
        assert got['host'] == at.removeprefix('http://') and 'te' not in got, 'the hop-by-hop headers stay behind'
        assert httpx.get(f'{url}/v1/echo').status_code == 201, 'a request with no body goes with none'
        assert holder.got[-1]['body'] == '' and 'transfer-encoding' not in holder.got[-1]
        chunked = httpx.post(f'{url}/v1/echo', content=iter([b'a', b'b']))
        assert chunked.json()['body'] == 'ab', 'a body of unknown length streams'
        large = 'x' * 500_000
        assert httpx.post(f'{url}/v1/echo', content=large.encode()).json()['body'] == large, 'a body of many parts'
        holder.go_on.set()
        streamed = httpx.get(f'{url}/v1/stream')
        assert (streamed.text, streamed.headers['x-from']) == ('one two', 'holder')
        refused = httpx.get(f'{url}/v1/refused')
        assert (refused.status_code, refused.json()['error']) == (409, 'refused'), "the holder's own 409 passes"
        assert httpx.get(f'{url}/v1/garbled').status_code == 409


def test_a_standby_forwards_nothing_twice_and_a_holder_serves_whatever_term_a_request_names(
    standby: tuple[Forwarding, str, Path], record: Record
) -> None:
    holder = Holder()
    _, url, _ = standby
    with serving(holder) as at:
        record.holds(7, at)
        forwards_to(url, 7)
        assert httpx.get(f'{at}/x', headers={'piloop-term': '1'}).status_code == 201, 'the holder ignores the header'
        again = httpx.get(f'{url}/x', headers={'piloop-term': '7'})
        assert (again.status_code, again.json()['error']) == (409, Problem.TERM), 'a standby never forwards it again'
        assert 'to this daemon, a standby' in again.json()['detail']
        assert len(holder.got) == 1, 'the refused request reached no holder'
    record.holds(8, url)  # a stale record: the term's address is a standby now, this one
    forwards_to(url, 8)
    stale = httpx.get(f'{url}/x')
    assert (stale.status_code, stale.json()['error']) == (503, Problem.UNAVAILABLE), 'one hop, then a retry'
    assert stale.headers['retry-after'] == '1' and f'{url} is a standby now: term 8 is over' in stale.json()['detail']


@contextmanager
def refusing() -> Generator[int]:
    """A port of 127.0.0.1 that refuses each connect, and that no other socket takes until the block ends.

    A socket that is bound and does not listen will not do: macOS drops what connects to it, and the connect times out.
    A connection that the port's listener accepted holds the port once the listener is closed.
    """
    with socket.create_server(('127.0.0.1', 0)) as listener:
        port = listener.getsockname()[1]
        client = socket.create_connection(('127.0.0.1', port))
        accepted, _ = listener.accept()
    with client, accepted:
        yield port


def test_without_a_holder_that_answers_or_while_it_starts_a_standby_answers_503(
    standby: tuple[Forwarding, str, Path], record: Record, capsys: pytest.CaptureFixture[str]
) -> None:
    forwarding, url, _ = standby
    none = httpx.get(f'{url}/v1/status')
    assert (none.status_code, none.json()['error']) == (503, Problem.UNAVAILABLE)
    assert none.headers['retry-after'] == '1'
    assert (ready(url).status_code, ready(url).json()) == (503, {'serves': False, 'role': 'standby', 'epoch': None})
    record.holds(1, None)
    forwards_to(url, 1)
    assert ready(url).status_code == 503, 'a holder that serves no other machine'
    assert 'serves no other machine' in httpx.get(f'{url}/v1/status').json()['detail']
    with refusing() as port:
        record.holds(2, f'http://127.0.0.1:{port}')
        forwards_to(url, 2)
        dead = httpx.get(f'{url}/v1/status')
    assert dead.status_code == 503 and 'did not answer (ConnectError)' in dead.json()['detail']
    reads = record.reads
    record.error = Unreachable('S3 is away')
    wait_until(lambda: record.reads > reads + 1, 'the standby reads the record again')
    assert ready(url).json()['epoch'] == 2, 'the term read last stands while the record cannot be read'
    assert 'the standby could not read the term: S3 is away' in capsys.readouterr().err
    forwarding.hold(3)
    wait_until(lambda: ready(url).json() == {'serves': False, 'role': 'starting', 'epoch': 3}, 'it starts its term')
    starting = httpx.get(f'{url}/v1/status')
    assert starting.status_code == 503 and 'took term 3, and starts' in starting.json()['detail']


def test_the_home_s_socket_on_a_standby_names_the_holder_and_forwards_nothing(
    standby: tuple[Forwarding, str, Path], record: Record
) -> None:
    _, url, path = standby
    record.holds(5, 'http://holder:8443')
    forwards_to(url, 5)
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), base_url='http://piloop') as local:
        assert local.get('/v1/ready').json() == {'serves': True, 'role': 'standby', 'epoch': 5}
        refused = local.get('/v1/status')
    assert (refused.status_code, refused.json()['error']) == (421, Problem.STANDBY)
    assert 'term 5 is held by host-5/1 (http://holder:8443)' in refused.json()['detail']


def test_a_new_term_ends_what_went_to_the_old_one_and_a_client_that_goes_ends_its_request(
    standby: tuple[Forwarding, str, Path], record: Record
) -> None:
    holder = Holder()
    _, url, _ = standby
    with serving(holder) as at:
        record.holds(1, at)
        forwards_to(url, 1)
        answers: list[httpx.Response] = []
        polling = threading.Thread(target=lambda: answers.append(httpx.get(f'{url}/hold', timeout=WAIT)))
        polling.start()
        wait_until(lambda: holder.got, 'the holder has the long-poll')
        streamed: list[bytes] = []
        failed: list[Exception] = []

        def stream() -> None:
            try:
                with httpx.stream('GET', f'{url}/stream', timeout=WAIT) as response:
                    for chunk in response.iter_raw():
                        streamed.append(chunk)
            except httpx.HTTPError as e:
                failed.append(e)

        cut = threading.Thread(target=stream)
        cut.start()
        wait_until(lambda: streamed, 'the stream started')
        record.holds(2, at)
        polling.join(WAIT)
        cut.join(WAIT)
        [ended] = answers
        assert (ended.status_code, ended.json()['error']) == (503, Problem.UNAVAILABLE)
        assert 'term 1 ended while its holder had the request' in ended.json()['detail']
        assert streamed == [b'one '] and len(failed) == 1, 'a stream that started is cut: its client sees it end early'
        assert holder.gone.wait(WAIT), 'the forwarder closed its request to the old term'
        holder.go_on.set()
    next_holder = Holder()
    with serving(next_holder) as at:
        record.holds(3, at)
        forwards_to(url, 3)
        with httpx.Client(timeout=0.5) as client, pytest.raises(httpx.ReadTimeout):
            client.get(f'{url}/hold')
        assert next_holder.gone.wait(WAIT), 'a client that went away ends the request to the holder'
        uploading = socket.create_connection(('127.0.0.1', int(url.rsplit(':', 1)[1])))
        uploading.sendall(b'POST /x HTTP/1.1\r\nhost: s\r\ncontent-length: 100\r\n\r\npart')
        uploading.close()  # before the rest of its body: the standby passes the cut on, and serves on
        assert httpx.get(f'{url}/v1/ready').status_code == 200


def test_a_failure_of_the_forwarder_itself_is_the_server_s_error(
    standby: tuple[Forwarding, str, Path], record: Record, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object) -> None:
        raise RuntimeError('as the test says')

    monkeypatch.setattr(forward_module, '_relay', broken)
    _, url, _ = standby
    record.holds(1, 'http://holder:1')
    forwards_to(url, 1)
    assert httpx.get(f'{url}/x').status_code == 500


def test_a_read_of_the_term_that_returns_after_the_close_lands_nowhere(tmp_path: Path) -> None:
    reading, returned, answer = threading.Event(), threading.Event(), threading.Event()

    def slow() -> Term | None:
        reading.set()
        answer.wait(WAIT)
        returned.set()
        return None

    listener = Listener(socket.create_server(('127.0.0.1', 0)), None)
    forwarding = Forwarding(slow, [listener], FAST)
    forwarding.start()
    assert reading.wait(WAIT)
    forwarding.close()  # it does not wait for the read
    answer.set()
    assert returned.wait(WAIT), 'the read ends on its own, into a loop that closed, and posts nothing there'
    listener.socket.close()


def test_a_standby_whose_socket_takes_no_connection_does_not_start(record: Record) -> None:
    with socket.socket(type=socket.SOCK_DGRAM) as datagrams:
        forwarding = Forwarding(record.current, [Listener(datagrams, None)], FAST)
        with pytest.raises(RuntimeError, match='the standby did not serve its sockets'):
            forwarding.start()
        forwarding.close()
    Forwarding(record.current, [], FAST).close()  # one that never started closes too


def test_a_holder_names_itself_to_the_others_by_this_host_s_name_when_it_listens_on_every_address() -> None:
    with socket.create_server(('0.0.0.0', 0)) as every, socket.create_server(('127.0.0.1', 0)) as one:
        assert advertised(Listener(every, None)) == f'http://{socket.getfqdn()}:{every.getsockname()[1]}'
        assert advertised(Listener(one, None)) == f'http://127.0.0.1:{one.getsockname()[1]}'


def test_a_409_is_the_refusal_of_a_term_only_when_its_body_says_so() -> None:
    assert _stale(b'{"error": "term", "detail": "x"}')
    assert not _stale(b'{"error": "refused"}')
    assert not _stale(b'[1]')
    assert not _stale(b'garbled')
