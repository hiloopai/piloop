"""``piloop cas serve``: a process of its own that serves the home's CAS, and what stops it from starting."""

import signal
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import cast

import httpx2 as httpx
import pytest
import uvicorn
from covalence import Cid, IntegrityError
from pytest_mock import MockerFixture
from rich.console import Console

from piloop.actor import System
from piloop.api.contract import Access
from piloop.cli import common
from piloop.db.session import Database
from piloop.net import cas_server
from piloop.net.tokens import Tokens
from piloop.plugins.cas.cached import Cached, CachedCas
from piloop.plugins.cas.disk import Disk, DiskCas
from piloop.plugins.cas.http import Http, HttpCas
from piloop.plugins.cas.kit.conformance import check_cas
from piloop.plugins.leader.contract import Leader, Unreachable
from piloop.plugins.network.https import TOKEN_ENV
from piloop.settings import Settings
from piloop.store.chunks import Chunked
from tests.cli.test_cli import run_cli
from tests.conftest import at_head
from tests.daemon.test_serve import env_for

PILOOP = [sys.executable, '-m', 'piloop']


@pytest.fixture
def out(tmp_path: Path, schema: Path, monkeypatch: pytest.MonkeyPatch):
    at_head(schema, tmp_path / 'piloop.db')
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path, uv_tool_dir=tmp_path / 'uv-tools'))
    console = Console(record=True, width=200, force_terminal=False)
    monkeypatch.setattr(common, 'console', console)
    return lambda: console.export_text(clear=True)


def test_cas_serve_prints_its_url_and_serves_until_a_ctrl_c(out, mocker: MockerFixture) -> None:
    served: list[socket.socket] = []
    real = uvicorn.Server.serve

    async def serve(self: uvicorn.Server, sockets: list[socket.socket]) -> None:
        served.extend(sockets)
        signal.raise_signal(signal.SIGINT)  # a Ctrl-C once it serves
        await real(self, sockets=sockets)

    mocker.patch.object(uvicorn.Server, 'serve', serve)
    run_cli(['cas', 'serve', '--listen', '127.0.0.1:0'])  # returns: the Ctrl-C ends it quietly
    assert [each.fileno() for each in served] == [-1], "the server's socket is closed"
    assert out().startswith('cas serving on http://127.0.0.1:')


@pytest.mark.parametrize(
    ('listen', 'refused'),
    [
        ('8443', "'8443' is not HOST:PORT"),
        ('0.0.0.0:8443', 'is not loopback, and without tls'),
    ],
)
def test_cas_serve_refuses_an_address_before_it_binds(out, listen: str, refused: str) -> None:
    with pytest.raises(SystemExit, match=refused):
        run_cli(['cas', 'serve', '--listen', listen])


def test_cas_serve_says_why_it_cannot_listen(out) -> None:
    with socket.create_server(('127.0.0.1', 0)) as taken:
        port = taken.getsockname()[1]
        with pytest.raises(SystemExit, match='the CAS does not listen: .*in use'):
            run_cli(['cas', 'serve', '--listen', f'127.0.0.1:{port}'])


def test_cas_serve_is_its_own_process_that_keeps_the_contract_for_a_worker(
    tmp_path: Path, schema: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / 'home'
    at_head(schema, home / 'piloop.db')
    env = env_for(home)
    with closing(Database(Settings(home=home).database_url, actor=System.HUMAN)) as db:
        secret = Tokens(db).mint('gpu1', Access.WORKER)
    monkeypatch.setenv(TOKEN_ENV, secret)
    with subprocess.Popen(
        [*PILOOP, 'cas', 'serve', '--listen', '127.0.0.1:0'], env=env, stdout=subprocess.PIPE, text=True
    ) as served:
        try:
            assert served.stdout is not None
            url = served.stdout.readline().removeprefix('cas serving on ').strip()
            remote = Http.model_validate({'url': url})
            worker = tmp_path / 'worker'
            for provider in (remote, Cached(remote=remote, local=Disk(path=worker / 'store'))):
                match provider.open(worker):
                    case (HttpCas(http=http) | CachedCas(remote=HttpCas(http=http))) as store:
                        with http:  # the client lives as long as the process, which is longer than this test
                            check_cas(store, tmp_path / 'scratch')
                    case other:
                        pytest.fail(f'{provider} opened {other}')
            assert len(list((home / 'store' / 'blake3').iterdir())) > 3, 'the home holds the blobs'
            with pytest.raises(httpx.HTTPStatusError):
                httpx.head(url + '/v1/cas/x').raise_for_status()
            served.send_signal(signal.SIGTERM)
            assert served.wait(timeout=30) == -signal.SIGTERM
        finally:
            served.kill()  # a no-op once it has stopped; else `with` would wait for it


def test_cas_get_prints_a_blob_or_a_chunked_file_and_names_what_the_cas_lacks(
    out, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes], mocker: MockerFixture
) -> None:
    cas = Disk().open(tmp_path)
    cid = cas.put(b'{"type":"user"}\n')
    run_cli(['cas', 'get', str(cid)])
    assert capsysbinary.readouterr().out == b'{"type":"user"}\n'
    run_cli(['cas', 'get', str(Chunked(chunks=(cid, cas.put(b'{"type":"assistant"}\n'))).put(cas))])
    assert capsysbinary.readouterr().out == b'{"type":"user"}\n{"type":"assistant"}\n', 'a chunked file, whole'
    gone = Chunked(chunks=(Cid.of(b'a part never put'),)).put(cas)
    with pytest.raises(SystemExit, match=f'{gone} is not a file that the CAS holds whole'):
        run_cli(['cas', 'get', str(gone)])
    missing = Cid.of(b'never put')
    with pytest.raises(SystemExit, match=f'the CAS has no blob {missing}'):
        run_cli(['cas', 'get', str(missing)])
    with pytest.raises(SystemExit, match='not-a-cid is not a blob address'):
        run_cli(['cas', 'get', 'not-a-cid'])
    mocker.patch.object(DiskCas, 'get', side_effect=IntegrityError('changed under the store'))
    with pytest.raises(SystemExit, match=f'{cid}: the bytes in the CAS do not match it'):
        run_cli(['cas', 'get', str(cid)])


def test_cas_serve_under_local_with_no_database_says_how_to_make_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `local` leader's database is the home's whether its daemon runs or not: it must exist."""
    monkeypatch.setattr(common, 'settings', Settings(home=tmp_path, uv_tool_dir=tmp_path / 'uv-tools'))
    monkeypatch.setattr(common, 'console', Console(record=True, width=200, force_terminal=False))
    with pytest.raises(SystemExit, match='piloop.db does not exist'):
        run_cli(['cas', 'serve', '--listen', '127.0.0.1:0'])


def test_a_cas_server_that_cannot_read_the_term_keeps_what_it_does(capsys: pytest.CaptureFixture[str]) -> None:
    class Unread:
        def here(self) -> bool:
            raise Unreachable('S3 is slow')

    leader = cast(Leader, Unread())
    assert cas_server._holds(leader, here=True) is True and cas_server._holds(leader, here=False) is False
    assert capsys.readouterr().err.count('piloop: the CAS server could not read the term: S3 is slow') == 2
