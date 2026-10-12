"""Workers as plain child processes: the headless runtime to use when Herdr is not available."""

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from piloop.proc import child_env
from piloop.runtime import Handle, NotSteerable, Outcome, WorkerSpec, WorkerStatus

STDOUT = 'worker.stdout'
STDERR = 'worker.stderr'
POLL_SECONDS = 0.1
SESSION_ID_SECONDS = 30
"""How long a live process has to print the line that ``session_id_pattern`` matches. After that, the session id is
unknown."""


def proc_stat(pid: int) -> tuple[str, int] | None:
    """Return ``(state, kernel start ticks)`` of ``pid`` from ``/proc``, or None when it is gone.

    A reused PID has different start ticks.
    """
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return None
    # Count the fields after the ")" that closes the command name: the command name can contain spaces.
    fields = stat.rpartition(')')[2].split()
    return fields[0], int(fields[19])


def proc_start_ticks(pid: int) -> int | None:
    """Return the kernel start time of ``pid``, or None when it is gone.

    A zombie still counts, because its PID is not reused yet.
    """
    stat = proc_stat(pid)
    return None if stat is None else stat[1]


class SubprocessRuntime:
    """One-shot harness processes. The prompt is in ``spec.args``, so ``prompt()`` is not supported.

    This instance keeps the processes it started, so it knows their exit codes. A handle loaded from the database in
    another process can still be observed and killed through ``/proc``, but has no exit code.
    """

    kind = 'subprocess'

    def __init__(self, project: str = '', project_path: Path | None = None) -> None:
        """Take the same arguments as every runtime constructor. A child process needs neither of them."""
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    def start(self, spec: WorkerSpec) -> Handle:
        """Start the process in its own session, with stdout and stderr written under ``spec.log_dir``."""
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        with (spec.log_dir / STDOUT).open('w') as out, (spec.log_dir / STDERR).open('w') as err:
            proc = subprocess.Popen(
                [spec.executable, *spec.args],
                cwd=spec.cwd,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                env=child_env() | spec.env,
                start_new_session=True,
            )
        self._procs[proc.pid] = proc
        return Handle(
            self.kind,
            {
                'pid': proc.pid,
                'pgid': os.getpgid(proc.pid),
                'start_ticks': proc_start_ticks(proc.pid),
                'started_at': time.time(),
                'log_dir': str(spec.log_dir),
                'session_id_pattern': spec.session_id_pattern,
            },
        )

    def wait(self, handle: Handle, timeout: float) -> Outcome:
        """Poll until the process is gone. On timeout, kill the whole process group."""
        started = time.monotonic()
        while self._alive(handle):
            if time.monotonic() - started > timeout:
                self.kill(handle)
                seconds = time.monotonic() - started
                return Outcome(WorkerStatus.TIMED_OUT, seconds, None, f'worker timeout after {timeout:g}s')
            time.sleep(POLL_SECONDS)
        rc = self._exit_code(handle)
        return Outcome(
            WorkerStatus.EXITED, time.monotonic() - started, rc, None if rc in (0, None) else f'worker exit {rc}'
        )

    def status(self, handle: Handle) -> WorkerStatus:
        """A live process is working. A one-shot process is never idle."""
        return WorkerStatus.WORKING if self._alive(handle) else WorkerStatus.EXITED

    def session_id(self, handle: Handle) -> str | None:
        """Return the first match of the spec's ``session_id_pattern`` in the captured stdout. None without a pattern.

        Poll while the process lives, up to ``SESSION_ID_SECONDS``. Read once more after it exits, so a process that
        prints nothing and ends causes no wait.
        """
        pattern = handle.data.get('session_id_pattern')
        if pattern is None:
            return None
        regex = re.compile(str(pattern))
        deadline = time.monotonic() + SESSION_ID_SECONDS
        while True:
            alive = self._alive(handle)
            found = regex.search(_stdout(handle))
            if found is not None:
                return found.group(1)
            if not alive or time.monotonic() >= deadline:
                return None
            time.sleep(POLL_SECONDS)

    def jobs(self, handle: Handle) -> list[str]:
        """Return no jobs: a one-shot process ends with its work, and what it leaves running is not tracked."""
        return []

    def prompt(self, handle: Handle, text: str) -> None:
        """Raise ``NotSteerable``: a one-shot process has no input channel."""
        raise NotSteerable(f'subprocess worker pid {_pid(handle)} cannot be steered; kill it and requeue the run')

    def read(self, handle: Handle, lines: int = 80) -> str:
        """Return the last ``lines`` lines of the captured stdout."""
        return '\n'.join(_stdout(handle).splitlines()[-lines:])

    def kill(self, handle: Handle) -> None:
        """Send SIGKILL to the process group. A group that is already gone is not an error."""
        if not self._alive(handle):
            return
        try:
            os.killpg(int(str(handle.data['pgid'])), signal.SIGKILL)
        except ProcessLookupError:
            return
        while self._alive(handle):
            time.sleep(POLL_SECONDS)

    def _alive(self, handle: Handle) -> bool:
        proc = self._procs.get(_pid(handle))
        if proc is not None:
            return proc.poll() is None
        stat = proc_stat(_pid(handle))
        return stat is not None and stat[0] != 'Z' and stat[1] == handle.data['start_ticks']

    def _exit_code(self, handle: Handle) -> int | None:
        proc = self._procs.get(_pid(handle))
        return proc.returncode if proc is not None else None


def _pid(handle: Handle) -> int:
    return int(str(handle.data['pid']))


def _log_dir(handle: Handle) -> Path:
    return Path(str(handle.data['log_dir']))


def _stdout(handle: Handle) -> str:
    path = _log_dir(handle) / STDOUT
    return path.read_text(errors='replace') if path.is_file() else ''
