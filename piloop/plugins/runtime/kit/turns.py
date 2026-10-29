"""The supervisor of an agent whose harness can continue its session: it runs the turns, and resumes while messages wait.

The runtime starts this module as the agent's process, the leader of its process group, with the agent's environment.
Each turn is a child in that group. So the agent's queue (``QUEUE``) has a reader for as long as the agent runs,
whichever daemon process watches it, also after ``piloop restart``. The turns come on stdin (``Turns``): the argv of
each turn is fixed at the start, and only the queued messages and the session id are filled in, so every turn sends
the harness's model the same prefix as the turn before, and its prompt cache holds.
"""

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from piloop.plugins.harness.contract import SESSION_ID, fill
from piloop.plugins.runtime.contract import take

SUPERVISOR_ARGV = (sys.executable, '-P', '-m', __name__)
"""What the runtime starts: this module's ``main``."""


class Turns(BaseModel):
    """What the supervisor runs: the harness's first turn, then a resumed turn for each batch of queued messages."""

    model_config = ConfigDict(frozen=True)

    prefix: list[str]
    """The command of a turn before the harness's arguments: the harness's executable, or a wrapper that ends with it,
    such as ``docker run``."""
    args: list[str]
    """The arguments of the first turn, the first prompt included."""
    resume_args: list[str]
    """The arguments of a resumed turn, with ``{prompt}`` and, when the harness reports its id, ``{session_id}``."""
    session_id_pattern: str | None
    """The regex that finds the harness's session id in ``stdout``."""
    stdout: Path
    """The agent's stdout, which the turns write."""
    queue: Path
    """The agent's queue of messages (``QUEUE``)."""

    def resumed(self) -> list[str] | None:
        """The command of the next turn, with the queued messages as its prompt. None when there are none.

        None too while the session id is unknown and the resume needs it: the messages then stay queued.
        """
        found = None if self.session_id_pattern is None else re.search(self.session_id_pattern, _read(self.stdout))
        session = '' if found is None else found.group(1)
        if not session and any(SESSION_ID in a for a in self.resume_args):
            return None
        if not (messages := take(self.queue)):
            return None
        prompt = '\n'.join(messages)
        return [*self.prefix, *(fill(a, prompt=prompt, session_id=session) for a in self.resume_args)]


def run(turns: Turns) -> int:
    """Run the first turn, then a resumed turn while the last one exited 0 and messages wait. Return the last status.

    A turn that a signal ended returns minus the signal, as ``subprocess`` reports it.
    """
    code = _turn([*turns.prefix, *turns.args])
    while code == 0 and (command := turns.resumed()) is not None:
        code = _turn(command)
    return code


def main() -> None:
    """Read the turns on stdin, run them, and end as the last turn ended: with its status, or by its signal."""
    code = run(Turns.model_validate_json(sys.stdin.buffer.read()))
    if code < 0:
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    raise SystemExit(code)


def _turn(command: list[str]) -> int:
    """Run one turn in this process's group, with its stdout, stderr, cwd and environment, and return its status."""
    return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode


def _read(path: Path) -> str:
    return path.read_text(errors='replace') if path.is_file() else ''


if __name__ == '__main__':
    main()
