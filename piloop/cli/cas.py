"""``piloop cas``: ``get`` prints a file of this home's CAS, and ``serve`` serves the CAS to workers on other machines."""

import contextlib
import signal
import sys
import threading
from typing import Annotated

import cyclopts
from covalence import IntegrityError
from pydantic import TypeAdapter, ValidationError

from piloop.cli import common, local
from piloop.plugins.cas.contract import BlobAddress
from piloop.plugins.leader.contract import LEADERS, LeaderHost
from piloop.plugins.network.contract import NETWORKS, Address
from piloop.store import chunks
from piloop.store.tree import Missing

app = cyclopts.App(name='cas', help="This home's CAS: print a blob, or serve the CAS to workers on other machines.")
ADDRESS: TypeAdapter[BlobAddress] = TypeAdapter(BlobAddress)


@app.command
def get(cid: Annotated[str, cyclopts.Parameter(help='The address of the file.')], /) -> None:
    """Print a file of the CAS of `[plugins.cas]` to stdout: a blob as stored, or a chunked file whole.

    An agent's session file (`agent_sessions.session_file`) is a chunked file.
    """
    try:
        address = ADDRESS.validate_python(cid)
    except ValidationError as e:
        raise SystemExit(f'{cid} is not a blob address: {e.errors()[0]["msg"]}') from e
    try:
        found = chunks.read(local.cas(), address)
    except IntegrityError as e:
        raise SystemExit(f'{address}: the bytes in the CAS do not match it') from e
    except (Missing, ValueError) as e:  # a part gone, or a value that is not a chunked file
        raise SystemExit(f'{address} is not a file that the CAS holds whole: {e}') from e
    if found is None:
        raise SystemExit(f'the CAS has no blob {address}')
    sys.stdout.buffer.write(found)


@app.command
def serve(
    *,
    listen: Annotated[
        str,
        cyclopts.Parameter(help='Serve on HOST:PORT. An address that is not loopback needs TLS ([plugins.network]).'),
    ],
) -> None:
    """Serve the CAS of `[plugins.cas]` until Ctrl-C or SIGTERM. Each caller sends a worker's token.

    Where the home's daemon does not hold its term (a standby under `[plugins.leader] provider = "bucket"`), it forwards
    each request to the CAS server of the daemon that does, at the same port, and answers 503 while none does.
    """
    # only now: `piloop cas get`, which an agent's call runs on the server, loads no web server
    from piloop.net import cas_server
    from piloop.net.server import url

    cfg = common.settings
    local.home()  # it serves this machine's home: refused while PILOOP_SERVER names another
    try:
        network = NETWORKS.selected(cfg.providers).listening(Address.model_validate(listen))
    except ValidationError as e:
        raise SystemExit(f'--listen {listen}: {e}') from e
    leader = LEADERS.selected(cfg.providers).open(LeaderHost(cfg.home, cfg.database))
    try:
        listener = cas_server.bind(cfg, network)
    except OSError as e:
        raise SystemExit(f'the CAS does not listen: {e}') from e
    common.console.print(f'cas serving on {url(listener)}', markup=False, highlight=False, soft_wrap=True)
    sys.stdout.flush()  # a service manager or a test reads the URL from a pipe
    stop = threading.Event()
    signalled: list[int] = []

    def on_signal(signum: int, _: object) -> None:
        signalled.append(signum)
        stop.set()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        cas_server.serve(cfg, network, listener, leader, stop)
    finally:
        listener.socket.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    with contextlib.suppress(KeyboardInterrupt):  # a Ctrl-C ends quietly; a SIGTERM ends the process as it would have
        signal.raise_signal(signalled[0])
