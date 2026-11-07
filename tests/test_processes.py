"""tests/processes.py: no process that a test starts outlives its module, also one that left its session or its parent."""

import json
import os
import subprocess
import sys
from pathlib import Path

from piloop import proc
from tests.conftest import wait_until
from tests.processes import SESSION, TAG_ENV

ROOT = Path(__file__).parent.parent
LEAKS = """import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PIDS = Path(os.environ['PIDS'])
# Each child ends by itself within a minute: should the reaping break, this test leaks nothing for long.
KEPT: list[subprocess.Popen[bytes]] = []  # else the GC warns that each still runs
BUSY = 'import time\\nend = time.monotonic() + 60\\nwhile time.monotonic() < end: pass'


def note(name: str, pid: int) -> None:
    pids = json.loads(PIDS.read_text()) if PIDS.exists() else {}
    PIDS.write_text(json.dumps({**pids, name: pid}))


@pytest.fixture(scope='module')
def module_server() -> int:
    server = subprocess.Popen(['sleep', '60'], start_new_session=True)
    KEPT.append(server)
    note('module', server.pid)
    return server.pid


def test_leaks_a_busy_loop_in_a_session_of_its_own_and_an_orphan(module_server: int) -> None:
    loop = subprocess.Popen([sys.executable, '-c', BUSY], start_new_session=True)
    KEPT.append(loop)
    note('loop', loop.pid)
    orphan = subprocess.run(['sh', '-c', 'sleep 60 >/dev/null 2>&1 & echo $!'], capture_output=True, text=True)
    note('orphan', int(orphan.stdout))


def test_ends_what_it_starts() -> None:
    subprocess.run(['true'], check=True)
"""


def test_a_test_that_leaves_processes_fails_and_nothing_it_started_survives_its_session(tmp_path: Path) -> None:
    (tmp_path / 'test_leaks.py').write_text(LEAKS)
    ran = subprocess.run(
        [sys.executable, '-m', 'pytest', '-c', str(ROOT / 'pyproject.toml'), '--rootdir', str(ROOT)]
        + ['-p', 'no:cacheprovider', '-p', 'tests.processes', '-q', str(tmp_path / 'test_leaks.py')],
        cwd=ROOT,
        env={**os.environ, 'PIDS': str(tmp_path / 'pids.json')},
        capture_output=True,
        text=True,
        timeout=120,
    )
    out = ran.stdout + ran.stderr
    assert ran.returncode == 1 and 'ERROR at teardown of test_ends_what_it_starts' in out, out
    assert '::test_leaks_a_busy_loop_in_a_session_of_its_own_and_an_orphan left pid' in out, out
    assert 'while time.monotonic() < end: pass' in out and 'sleep 60' in out, out
    assert 'test_ends_what_it_starts left' not in out, out
    pids: dict[str, int] = json.loads((tmp_path / 'pids.json').read_text())
    assert pids.keys() == {'module', 'loop', 'orphan'}
    for name, pid in pids.items():  # init reaps the orphans that the kill left as zombies
        wait_until(lambda pid=pid: proc.started(pid) is None, f'the end of the {name} process')


def test_a_process_that_a_test_starts_carries_the_test_s_tag() -> None:
    tag = subprocess.run(['sh', '-c', f'echo "${TAG_ENV}"'], capture_output=True, text=True, check=True).stdout
    assert tag == f'{SESSION} tests/test_processes.py::test_a_process_that_a_test_starts_carries_the_test_s_tag\n'
