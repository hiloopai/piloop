"""Child processes: the environment they get, and how to kill a whole process group."""

import contextlib
import os
import signal
import subprocess

STDERR_IN_ERROR = 500
"""The number of characters of a failed command's stderr that piloop keeps in the error it stores on a row."""
UNITS_ENV = 'PILOOP_UNITS_FILE'
"""Where the evaluate command writes its per-unit results. Set only for the daemon's own evaluations."""


def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Send SIGKILL to the whole session of the child, which is its leader. Do nothing if the group is already gone."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def child_env(*drop: str) -> dict[str, str]:
    """The environment for subprocesses: this process's environment without VIRTUAL_ENV, ``UNITS_ENV`` and ``drop``.

    Without VIRTUAL_ENV, uv does not warn about the project's own virtual environment. Only ``run_evaluate`` sets
    ``UNITS_ENV``, so no agent gets a units path.

    This is the only place outside ``Settings`` that reads the environment. It passes the values on and does not
    interpret them.
    """
    return {k: v for k, v in os.environ.items() if k not in ('VIRTUAL_ENV', UNITS_ENV) and k not in drop}
