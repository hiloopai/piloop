"""``piloop secret set|list|rm``, clients of the API: the secrets that agents get in their environment.

No command shows a value.
"""

import getpass
import sys
from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

import cyclopts
from pydantic import SecretStr, TypeAdapter, ValidationError
from rich import box
from rich.table import Table

from piloop.api.client import Client, NoDaemon
from piloop.api.contract import Located, SecretBody
from piloop.cli import common
from piloop.cli.client import cwd, daemon_http
from piloop.ids import EnvName

app = cyclopts.App(name='secret', help='The secrets that agents get in their environment. No command shows a value.')

Name = Annotated[str, cyclopts.Parameter(help='The name of the secret: the variable that agents get it in.')]
CampaignOption = Annotated[
    str | None,
    cyclopts.Parameter(help="The campaign whose secret it is, over the home's of the same name. Unset: the home's."),
]
ENV_NAME: TypeAdapter[str] = TypeAdapter(EnvName)


@app.command(name='set')
def secret_set(name: Name, /, *, campaign: CampaignOption = None) -> None:
    """Set a secret, in place of any, from stdin: `printf %s "$KEY" | piloop secret set EXA_API_KEY`.

    On a terminal, it asks for the value and does not show it. Agents get the value from their next start. An agent
    sets only its own campaign's secrets.
    """
    variable = _name(name)
    value = getpass.getpass(f'{variable}: ') if sys.stdin.isatty() else sys.stdin.read().removesuffix('\n')
    if not value:
        raise SystemExit(f'{variable}: the value is empty')
    with _daemon() as client:
        changed = client.set_secret(variable, _owner(campaign), SecretBody(value=SecretStr(value)))
    common.console.print(f'{variable} set for {_whose(changed.campaign)}', markup=False, highlight=False)


@app.command(name='list')
def secret_list() -> None:
    """The secrets: the name, whose it is, and who set it last and when. Never a value."""
    with _daemon() as client:
        listed = client.list_secrets().secrets
    table = Table('name', 'of', 'set', 'by', box=box.SIMPLE)
    for found in listed:
        at = f'{found.set_at:%Y-%m-%d %H:%M}' if found.set_at else ''
        table.add_row(found.name, found.campaign or 'home', at, found.set_by or '')
    common.console.print(table)


@app.command(name='rm')
def secret_rm(name: Name, /, *, campaign: CampaignOption = None) -> None:
    """Delete a secret. An agent that starts after it no longer gets it."""
    variable = _name(name)
    with _daemon() as client:
        changed = client.remove_secret(variable, _owner(campaign))
    common.console.print(f'{variable} of {_whose(changed.campaign)} deleted', markup=False, highlight=False)


@contextmanager
def _daemon() -> Generator[Client]:
    """The client of the daemon. Secrets go through its API only, as on a server, which has nothing else."""
    try:
        http = daemon_http()
    except NoDaemon as e:
        raise SystemExit(
            "`piloop secret` goes through the daemon's API: the daemon checks who may set each secret and records who"
            f' did, here as on a server. No daemon listens on {common.settings.socket_file}: start it with'
            ' `piloop serve --detach`, then run this again. A paused campaign stays paused.'
        ) from e
    with Client.connect(http) as client:
        yield client


def _name(name: str) -> str:
    """``name``, checked as the route checks it before a value is read."""
    try:
        return ENV_NAME.validate_python(name)
    except ValidationError as e:
        raise SystemExit(
            f'{name!r} is not an environment variable name: letters, digits and _, not first a digit'
        ) from e


def _owner(campaign: str | None) -> Located:
    return Located(campaign=campaign, cwd=None if campaign is None else cwd())


def _whose(campaign: str | None) -> str:
    return 'the home' if campaign is None else f'campaign {campaign}'
