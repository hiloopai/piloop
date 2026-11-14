"""The template of the relayed calls (``piloop.plugins.executor.kit.forks``): the process that forks one per call.

The tests drive it through ``Forks``: coverage measures the template and its children (``patch``).
"""

import gc
import os
import signal
import socket
import sys
import threading
from typing import NoReturn

from piloop.plugins.executor.kit.forks import GO, REQUEST_BYTES, THREADED, Request


def serve(fd: int) -> None:
    """The template: import the relayed commands, then fork a child for each request on the socket ``fd``.

    It returns once the server closes its end. In a child, it raises ``SystemExit`` with the call's exit code. A fork
    copies only the thread that forks: with another thread, which may hold a lock that the child then waits for, it
    exits with ``THREADED`` before the fork, and the server runs its calls in processes of their own.
    """
    control = socket.socket(fileno=fd)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # the kernel reaps the children: each reports to the server itself
    _preload()
    gc.freeze()  # the collector never walks what the template made: a child's GC and its exit stay short
    while True:
        message, fds, _, _ = socket.recv_fds(control, REQUEST_BYTES, 4)
        if not message:
            return
        if (threads := threading.active_count()) != 1:
            print(f'piloop: the template of the relayed calls runs {threads} threads and forks none', file=sys.stderr)
            raise SystemExit(THREADED)
        sys.stdout.flush()
        sys.stderr.flush()
        if os.fork() == 0:
            _forked(control, message, fds)
        for each in fds:
            os.close(each)


def _forked(control: socket.socket, message: bytes, fds: list[int]) -> NoReturn:
    """The child of a call: it ends with the call's exit code, as the interpreter ends a ``piloop`` process."""
    control.close()
    raise SystemExit(_child(Request.model_validate_json(message), *fds))


def _preload() -> None:
    """Import what the relayed commands import before they run: the CLI, and the module of each command."""
    from piloop.cli.main import app  # the template's work: the server never needs it
    from piloop.plugins.role.contract import RELAYED

    for verb in RELAYED:
        command = app
        for word in verb:
            command = command[word]  # cyclopts imports a command that is named by its path


def _child(request: Request, stdin: int, stdout: int, stderr: int, reply: int) -> int:
    """Run the call as a ``piloop`` process runs it, and return its exit code, which ``reply`` gets too.

    It sends its pid, and runs only once the server, which read it, answers ``GO``: a call that the server stopped
    first, at its deadline, never runs.
    """
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)  # a command waits for its own children
    os.setsid()
    # No process that the command starts may hold these: the server waits for ``reply`` to close. The copies that
    # dup2 makes on 0, 1 and 2 stay inheritable.
    for fd in (stdin, stdout, stderr, reply):
        os.set_inheritable(fd, False)
    for fd, to in ((stdin, 0), (stdout, 1), (stderr, 2)):
        os.dup2(fd, to)
        os.close(fd)
    os.write(reply, str(os.getpid()).encode())
    try:
        go = os.read(reply, len(GO))
    except ConnectionResetError:  # the server stopped the call and closed its end with the pid unread
        go = b''
    if go != GO:  # the call never runs: the server answered it already
        return 1
    os.chdir(request.cwd)
    os.environ.clear()
    os.environ.update(request.env)
    code = _main(['piloop', *request.argv])
    sys.stdout.flush()
    sys.stderr.flush()
    os.write(reply, str(code).encode())
    return code


def _main(argv: list[str]) -> int:
    """``piloop.cli.main.main`` with ``argv``, after it made ``settings`` and the console again, and its exit code.

    ``SystemExit`` gives its code as the interpreter does: none is 0, a message goes to stderr and is 1. Another
    error ends the child as it ends the interpreter, with its traceback: the server reads no code, which is 1.
    """
    from piloop.cli import common  # preloaded
    from piloop.cli.main import main
    from piloop.problems import HomeConfigError
    from piloop.settings import reload, settings

    sys.argv = argv
    code: object = None  # the CLI ends with SystemExit, but a return is a 0 too
    try:
        reload(settings)  # from this call's environment, as ``piloop.cli.run`` reads them
        common.console = common.output_console()
        main()
    except HomeConfigError as e:
        code = f'piloop: {e}'
    except SystemExit as e:
        code = e.code
    if code is None or isinstance(code, int):
        return code or 0
    print(code, file=sys.stderr)
    return 1
