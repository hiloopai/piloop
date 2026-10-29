"""Agents as plain background processes: the built-in runtime."""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from piloop import deprecated
from piloop.plugins.runtime.contract import (
    MESSAGES_ENV,
    QUEUE,
    TAG_ENV,
    AgentExit,
    AgentSpec,
    Deadline,
    ExitStatus,
    Handle,
    NotSteerable,
    RuntimeConfig,
    put,
)
from piloop.plugins.runtime.kit.tags import kill_tagged
from piloop.plugins.runtime.kit.turns import SUPERVISOR_ARGV, Turns
from piloop.proc import Leader, Started, child_env, kill_group, running, started

STDOUT = 'agent_session.stdout'
STDERR = 'agent_session.stderr'
POLL_SECONDS = 0.1
SESSION_ID_SECONDS = 30
"""How long a live process has to print the line that ``session_id_pattern`` matches. After that, the session id is
unknown."""
TURNS = 'turns'
"""The handle's key of an agent that a supervisor runs (``kit.turns``), with the version of its protocol: 1."""
CURRENT = 'agent_session.current'
"""Under the log directory of an agent that piloop 0.4 started: the pid and start time of its latest turn, once a
resumed turn replaced its first process."""


class Subprocess(RuntimeConfig):
    """``subprocess``: each agent as a background process on this machine. It has no settings, and it is the default."""

    DEFAULT = True

    def open(self, name: str) -> SubprocessRuntime:
        return SubprocessRuntime(name)


class SubprocessRuntime:
    """Each agent as a background process without a terminal, the leader of its own process group.

    The first prompt is in ``spec.args``.

    A message (``prompt``) waits in a queue. A hook of the harness can take it during the turn (``MESSAGES_ENV``).
    Otherwise the next turn takes it: an agent whose harness has ``spec.resume_args`` runs under a supervisor
    (``kit.turns``), which starts the harness again when a turn ends with messages queued, to continue the same session
    with them as the prompt. The supervisor is the agent's process and holds its environment, which is never written
    down, so any instance can queue a message for it: also one that re-attached to it after ``piloop restart``.

    This instance keeps the processes it started, so it knows their exit codes. A handle loaded from the database in
    another process can still be observed and killed by its pid and start time, but has no exit code.
    """

    def __init__(self, name: str) -> None:
        """``name``: the runtime's, which its handles carry."""
        self.name = name
        self._procs: dict[int, subprocess.Popen[bytes]] = {}
        """The process of each agent this instance started, by its pid (the handle's)."""

    def start(self, spec: AgentSpec) -> Handle:
        """Start the agent in its own session, with stdout and stderr written under ``spec.log_dir``.

        Its environment has ``TAG_ENV``, which every process it starts inherits, so ``kill`` finds them all. An agent
        whose harness has resume arguments starts as its supervisor, which gets its ``Turns`` on stdin.
        """
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        command = self._command(spec, token)
        turns = None
        if spec.resume_args:
            turns = Turns(
                prefix=command.argv,
                args=spec.args,
                resume_args=spec.resume_args,
                session_id_pattern=spec.session_id_pattern,
                stdout=spec.log_dir / STDOUT,
                queue=spec.log_dir / QUEUE,
            )
            command = Command(list(SUPERVISOR_ARGV), command.env)
        else:
            command = Command([*command.argv, *spec.args], command.env)
        proc = _spawn(spec, command, turns is not None)
        try:
            if turns is not None:
                assert proc.stdin is not None  # a pipe
                with proc.stdin:
                    proc.stdin.write(turns.model_dump_json().encode())
            handle = Handle(
                self.name,
                {
                    'pid': proc.pid,
                    # start_new_session makes the child its group's leader. os.getpgid would ask the system, and
                    # macOS refuses (ESRCH) for a child that already exited, which failed the start of a quick agent.
                    'pgid': proc.pid,
                    'start_time': started(proc.pid),
                    'started_at': time.time(),
                    'log_dir': str(spec.log_dir),
                    'session_id_pattern': spec.session_id_pattern,
                    'token': token,
                    **({TURNS: 1} if turns is not None else {}),
                },
            )
        except BaseException:
            # No handle reaches the caller, so nothing would wait for this process: end it here.
            kill_group(proc)
            proc.wait()
            raise
        self._procs[proc.pid] = proc
        return handle

    def wait(self, handle: Handle, deadline: Deadline) -> AgentExit:
        """Poll until the agent is gone: its last turn ended, and no message waits for another.

        At the deadline, kill its process group: all its turns share the deadline.
        """
        started = time.monotonic()
        while self._alive(handle):
            if deadline.passed():
                self.kill(handle)
                seconds = time.monotonic() - started
                error = f'agent timeout after {deadline.seconds():g}s'
                return AgentExit(ExitStatus.TIMED_OUT, seconds, None, error)
            time.sleep(POLL_SECONDS)
        rc = self._exit_code(handle)
        return AgentExit(
            ExitStatus.EXITED, time.monotonic() - started, rc, None if rc in (0, None) else f'agent exit {rc}'
        )

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

    def prompt(self, handle: Handle, text: str) -> None:
        """Keeps the message in a queue, which a hook of the harness can take in the turn, or else the next turn takes.

        When the turn ends with messages queued, piloop runs the harness again with
        ``[plugins.harness.<name>] resume``, with the queued messages as the prompt. A harness without ``resume`` takes
        no message.

        Note:
            The queue is ``QUEUE``: the process of this turn has no input channel. Raise ``NotSteerable`` when nothing
            would take the message: no supervisor runs the agent (its harness cannot continue its session, or an older
            piloop started it), or the agent has ended. The append holds the queue's lock, so a turn that starts
            meanwhile takes the message or leaves it for the next one.
        """
        if TURNS not in handle.data:
            raise NotSteerable(
                f'subprocess agent pid {_pid(handle)} cannot take a message: its harness has no resume arguments, or'
                ' an older piloop started it'
            )
        if not self._alive(handle):
            raise NotSteerable(f'subprocess agent pid {_pid(handle)} has ended')
        put(_log_dir(handle), text)

    def _command(self, spec: AgentSpec, token: str) -> Command:
        """The command of each turn before the harness's arguments, and its environment.

        The command is the harness's executable, and the environment has the agent's tag and queue.
        """
        env = child_env() | spec.env | {TAG_ENV: token, MESSAGES_ENV: str(spec.log_dir / QUEUE)}
        return Command([spec.executable], env)

    def read(self, handle: Handle, lines: int = 80) -> str:
        """Return the last ``lines`` lines of the captured stdout."""
        return '\n'.join(_stdout(handle).splitlines()[-lines:])

    def kill(self, handle: Handle) -> None:
        """Send SIGKILL to the process group, then to every process that carries the agent's process tag.

        The handle keeps the tag under its ``token`` key: a handle is stored, and its agent can outlive an upgrade.

        Harnesses run tool commands in sessions of their own (Claude Code's Bash calls), outside the group, and those
        outlive the agent. A group or a process that is already gone is not an error.
        """
        if self._alive(handle):
            kill_group(self._leader(handle))  # each process leads its own group: pgid = pid
            while self._alive(handle):
                time.sleep(POLL_SECONDS)
        kill_tagged(f'{TAG_ENV}={handle.data["token"]}')

    def _alive(self, handle: Handle) -> bool:
        return running(self._leader(handle))

    def _leader(self, handle: Handle) -> Leader:
        """The agent's process: its ``Popen`` when this instance started it."""
        proc = self._procs.get(_pid(handle))
        return proc if proc is not None else Started(*_current(handle))

    def _exit_code(self, handle: Handle) -> int | None:
        proc = self._procs.get(_pid(handle))
        return proc.returncode if proc is not None else None


def _pid(handle: Handle) -> int:
    return int(str(handle.data['pid']))


def _current(handle: Handle) -> tuple[int, float]:
    """The pid and start time of the agent's process: the handle's, or ``CURRENT`` of a resumed agent of piloop 0.4."""
    path = _log_dir(handle) / CURRENT
    data = handle.data
    if path.is_file():
        deprecated.until(
            '0.6.0', 'an agent of piloop 0.4 names its resumed turn in CURRENT: delete CURRENT and this branch'
        )
        data = json.loads(path.read_text())
    return int(str(data['pid'])), float(str(data['start_time']))


@dataclass(frozen=True)
class Command:
    """What one turn of an agent runs on this machine."""

    argv: list[str]
    env: dict[str, str]


def _spawn(spec: AgentSpec, command: Command, supervised: bool) -> subprocess.Popen[bytes]:
    """Start the agent in a session of its own, writing its output files. A supervisor gets a pipe on stdin."""
    with (spec.log_dir / STDOUT).open('w') as out, (spec.log_dir / STDERR).open('w') as err:
        return subprocess.Popen(
            command.argv,
            cwd=spec.cwd,
            stdout=out,
            stderr=err,
            stdin=subprocess.PIPE if supervised else subprocess.DEVNULL,
            env=command.env,
            start_new_session=True,
        )


def _log_dir(handle: Handle) -> Path:
    return Path(str(handle.data['log_dir']))


def _stdout(handle: Handle) -> str:
    path = _log_dir(handle) / STDOUT
    return path.read_text(errors='replace') if path.is_file() else ''
