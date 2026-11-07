"""No process that a test starts outlives its module, and the test that left one fails.

Each pytest process sets ``TAG_ENV`` to a token of its own, and ``tagged_by_its_test`` sets it to the token and the
test's node id while the test runs. Every process that the test starts inherits it: also one in a session or a process
group of its own, and one whose parent died. ``reaped_with_its_module`` ends what the tests of its module left, and
fails the last of them with each process it found. At exit, the pytest process ends every process it tagged, so a run
that Ctrl-C or an internal error stops leaves nothing. A SIGKILL of pytest does.

A process that a test starts with an environment of its own, not this one's, has no tag. The e2e campaigns start from
``tests.isolation.CALLER_ENV``, and their fixtures end them.
"""

import atexit
import os
import secrets
from collections.abc import Iterator

import psutil
import pytest

from piloop.plugins.runtime.kit.tags import kill_tagged

TAG_ENV = 'PILOOP_TEST_TAG'
"""The tag of a process that a test started: this pytest process's ``SESSION``, then the test's node id."""
SESSION = secrets.token_hex(8)
"""This pytest process's token. A worker of pytest-xdist has a token of its own."""


def end(prefix: str) -> list[str]:
    """End every process whose tag starts with ``prefix``, and return a line for each: its test, pid and command."""
    found: dict[str, list[str]] = {}
    for proc in psutil.process_iter():
        try:
            tag = proc.environ().get(TAG_ENV, '')
            if tag.startswith(prefix) and proc.status() != psutil.STATUS_ZOMBIE:
                found.setdefault(tag, []).append(f'pid {proc.pid}, {" ".join(proc.cmdline())}')
        except psutil.Error:  # another user's process, or one that ended while it was read
            continue
    for tag in found:
        kill_tagged(f'{TAG_ENV}={tag}')
    return [f'{tag.removeprefix(SESSION).strip() or "the session"} left {p}' for tag, ps in found.items() for p in ps]


def pytest_configure(config: pytest.Config) -> None:
    os.environ[TAG_ENV] = SESSION
    atexit.register(end, SESSION)


@pytest.fixture(scope='module', autouse=True)
def reaped_with_its_module(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if left := end(f'{SESSION} {request.node.nodeid}::'):
        pytest.fail('a test left processes running, now killed:\n' + '\n'.join(left), pytrace=False)


@pytest.fixture(autouse=True)
def tagged_by_its_test(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TAG_ENV, f'{SESSION} {request.node.nodeid}')
