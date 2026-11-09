"""Run piloop's tests and print ``passed``, how many pass, and ``failed``, how many fail.

``python3 evaluate.py [pytest args]`` installs the locked dependencies from uv's cache into ``.venv`` (no network) and
runs pytest on the arguments, by default every test of ``tests/``, the ones you add included, on all the CPUs. A test
of ``SANDBOX_FAILS``, which fails in some sandbox at the start, counts neither way, so the counts are the same in every
sandbox. The tests run without the
``PILOOP_*`` variables of the shell, which would point the suite at a live piloop.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = '/opt/uv-cache'
"""uv's cache in the image, which holds the locked dependencies (``uv.toml``)."""
SANDBOX_FAILS = (
    'tests/archive/test_archiver.py::test_the_upkeep_reports_a_file_it_cannot_read_and_counts_from_its_end',
    'tests/archive/test_store.py::test_a_file_a_prune_cannot_read_keeps_its_archiving_unpruned',
    'tests/archive/test_store.py::test_a_file_that_cannot_be_read_when_nothing_fits_is_reported_with_why',
    'tests/store/test_tree.py::test_what_cannot_be_read_is_skipped_and_left_in_place',
    'tests/store/test_tree.py::test_remove_leaves_a_file_it_cannot_read',
    'tests/store/test_tree.py::test_without_reflinks_trees_share_hard_links_to_read_only_copies',
    'tests/campaign/test_campaigns.py::test_create_campaign_repo',
    'tests/cli/test_cli.py::test_init_blank_then_register_then_reread',
    'tests/cli/test_cli.py::test_init_names_the_campaign_once',
    'tests/cli/test_cli.py::test_sql_and_status_while_another_client_holds_the_write_lock',
    'tests/db/test_sql.py::test_a_write_the_lock_refuses_names_the_holder',
    'tests/cli/test_doctor.py::test_a_healthy_install_passes_every_check',
    'tests/test_generate_all.py::test_each_generated_file_keeps_the_current_side_in_a_merge',
)
"""The tests that fail in some sandbox at the start, which do not count. The first six need a user whom file
permissions bind (the sandboxes run as root), the next three a network that reaches PyPI. The next three fail on
Modal's sandboxes, which run on gVisor: two name the holder of SQLite's write lock from ``/proc/locks``, which gVisor
does not list, and the doctor's healthy install failed there for a reason its scoring did not record. The last reads
git's attributes of the repo, which the tree that a scorer measures, an export without ``.git``, has none of."""
PLUGIN = """
import json, os

_config = None


def pytest_configure(config):
    global _config
    _config = config


def _write(row):
    if not hasattr(_config, 'workerinput'):  # xdist sends each report to the controller too
        with open(os.environ['T9_OUTCOMES'], 'a') as f:
            f.write(json.dumps(row) + '\\n')


def pytest_runtest_logreport(report):
    _write([report.nodeid, report.when, report.outcome])


def pytest_collectreport(report):
    if report.failed:
        _write([report.nodeid, 'collect', 'failed'])
"""
"""A pytest plugin that writes the outcome of each phase of each test, and each module that fails to collect."""


def environment() -> dict[str, str]:
    """This process's environment, without piloop's variables and pytest's own options."""
    return {k: v for k, v in os.environ.items() if not k.startswith(('PILOOP_', 'PYTEST_'))}


def cpus() -> int:
    """The CPUs that this process may use: its cgroup's quota, when it has one, within the CPUs it may run on."""
    n = len(os.sched_getaffinity(0))
    quota = Path('/sys/fs/cgroup/cpu.max')
    if quota.exists():
        limit, period = quota.read_text().split()
        if limit != 'max':
            n = min(n, max(1, int(limit) // int(period)))
    return n


def sync(root: Path) -> None:
    """Install the locked dependencies and the repo itself into ``root/.venv``, from uv's cache.

    Raises:
        RuntimeError: uv could not install them, with what it said.
    """
    done = subprocess.run(
        ['uv', 'sync', '--frozen', '--offline', '--cache-dir', CACHE, '--link-mode', 'symlink', '-q'],
        cwd=root,
        env=environment(),
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise RuntimeError(f'uv sync failed: {done.stderr.strip()[-2000:]}')


def outcomes(root: Path, args: list[str], *, config: Path | None = None) -> dict[str, str]:
    """Run pytest in ``root`` on ``args``: ``passed``, ``failed`` or ``skipped`` for each node id that it ran.

    A test fails when any of its phases fails, and a module that fails to collect is a failed node. ``config`` replaces
    the repo's pytest configuration.

    Raises:
        RuntimeError: pytest stopped short of running the tests (an error of its own, or of its arguments).
    """
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / 't9_outcomes.py').write_text(PLUGIN)
        log = Path(tmp) / 'outcomes.jsonl'
        log.touch()
        env = environment()
        env['PYTHONPATH'] = os.pathsep.join(p for p in (tmp, env.get('PYTHONPATH')) if p)
        env['T9_OUTCOMES'] = str(log)
        own = ['-c', str(config), '--rootdir', str(root)] if config is not None else []
        argv = [str(root / '.venv' / 'bin' / 'python'), '-m', 'pytest', '-p', 'no:cacheprovider', '-p', 't9_outcomes']
        argv += [*own, '-q', '--continue-on-collection-errors', *args]
        done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
        found: dict[str, str] = {}
        for line in log.read_text().splitlines():
            nodeid, when, outcome = json.loads(line)
            if outcome == 'failed':
                found[nodeid] = 'failed'
            elif when == 'call' or outcome == 'skipped':
                found.setdefault(nodeid, outcome)
        if done.returncode not in (0, 1):
            raise RuntimeError(f'pytest exited {done.returncode}: {(done.stdout + done.stderr).strip()[-2000:]}')
        return found


def main() -> int:
    """Print the metrics as one JSON line, or the reason of the failure on stderr."""
    t0 = time.monotonic()
    try:
        sync(ROOT)
        found = outcomes(ROOT, sys.argv[1:] or ['tests', '-n', str(cpus())])
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    metrics: dict[str, float] = {
        'passed': sum(o == 'passed' and n not in SANDBOX_FAILS for n, o in found.items()),
        'failed': sum(o == 'failed' and n not in SANDBOX_FAILS for n, o in found.items()),
        'seconds': round(time.monotonic() - t0, 1),
    }
    line = json.dumps({'status': 'ok', 'score': metrics.pop('passed'), 'direction': 'higher', 'metrics': metrics})
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
