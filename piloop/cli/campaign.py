"""The commands about campaigns: ``init``, and those that open the database: ``history``, ``campaign parent|tree``."""

import sys
from pathlib import Path
from typing import Annotated

import cyclopts
from pydantic import SecretStr

from piloop.api.client import Client, answers
from piloop.api.contract import (
    MAX_POLL_SECONDS,
    Checkout,
    Credential,
    EventsQuery,
    Fork,
    SecretCredential,
    Selection,
    SyncBody,
    TokenCredential,
)
from piloop.api.views import Registered
from piloop.campaign import ops
from piloop.campaign.campaigns import (
    BLANK,
    EVALUATE_CONTRACT,
    EVALUATE_STUB,
    EXAMPLE_DATA,
    TEMPLATES,
    create_campaign_repo,
    stub_untouched,
    tree as campaign_tree,
)
from piloop.cli import common, daemon, local
from piloop.cli.client import campaign_name, cwd, daemon_http
from piloop.cli.common import CampaignRef
from piloop.config import load_campaign_config
from piloop.db.models import EVENT
from piloop.db.triggers.changelog import CampaignPausedChanged, CampaignStatusChanged
from piloop.ids import CampaignStatus, checked_campaign_name
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded, derive
from piloop.text import metric


def init(
    path: Annotated[Path, cyclopts.Parameter(help='The campaign directory.')] = Path(),
    /,
    *,
    example: Annotated[
        str | None,
        cyclopts.Parameter(help=f'Write a bundled example instead of the blank template: {", ".join(EXAMPLE_DATA)}.'),
    ] = None,
    name: Annotated[
        str | None, cyclopts.Parameter(help='Register the campaign under this name, not the `name` of piloop.toml.')
    ] = None,
) -> None:
    """Make a directory a piloop campaign and register it.

    It is safe to run again. When needed, it runs `git init` and makes a baseline commit. When there is no
    piloop.toml, it writes piloop.toml, brief.md and evaluate.py from the template, and keeps the files that exist.
    While evaluate.py is still the template stub, it stops and says what to edit. Then it starts the daemon, unless
    one runs, registers the campaign with HEAD as its baseline candidate, and waits while the daemon measures it. A
    baseline that fails to evaluate pauses the campaign, and init exits with why. Ctrl-C stops only the wait: the
    campaign stays registered. For a campaign that is registered already, it reads piloop.toml again.
    """
    if example is not None and example not in EXAMPLE_DATA:
        raise SystemExit(f'no example {example!r}; bundled: {", ".join(EXAMPLE_DATA)}')
    try:
        named = None if name is None else checked_campaign_name(name)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    written = create_campaign_repo(path, TEMPLATES / example if example else BLANK, EXAMPLE_DATA.get(example or '', {}))
    path = path.resolve()
    for rel in written:
        common.console.print(f'wrote {rel}')
    if stub_untouched(path):
        common.console.print(EVALUATE_CONTRACT, style='dim', markup=False)
        common.console.print(
            f'next: {path / EVALUATE_STUB} is still the template stub. Make it measure this campaign and report the'
            f' metrics that piloop.toml names. Describe the goal in brief.md. Then run `piloop init {path}` again.'
        )
        return
    if common.settings.server is None and not answers(common.settings.socket_file):
        daemon.detached()
    with Client.connect(daemon_http()) as client:
        found = next((c for c in client.status(Selection()).campaigns if c.path == str(path)), None)
        if found is None:
            added = client.register_campaign(Checkout(path=str(path), name=named))
            campaign, documents = added.campaign, added.documents
            # the measurement waits for a free evaluation slot, which can be on a worker that has not connected yet
            common.console.print(
                f'campaign [bold]{campaign.name}[/] #{campaign.id} registered; measuring baseline'
                f' candidate#{added.baseline} (`piloop eval queue` shows it; Ctrl-C stops only the wait)'
            )
            ratios = {m.name: m.ratio for m in load_campaign_config(path).metrics if m.ratio is not None}
            metrics = derive(_measured(client, added), ratios)
            common.console.print(
                f'baseline candidate#{added.baseline}:'
                f' {", ".join(f"{name} {metric(value)}" for name, value in metrics.items())}'
            )
            common.console.print(f'next: add ideas: `piloop idea add {campaign.name} "<title>"`')
        elif named not in (None, found.name):
            raise SystemExit(f'{path} is campaign {found.name} #{found.id} already: a campaign keeps its name')
        else:
            synced = client.sync_campaign(found.name)
            campaign, documents = synced.campaign, synced.documents
            resumed = (
                '; resumed: stop conditions changed'
                if synced.was is CampaignStatus.STOPPED and campaign.status is CampaignStatus.ACTIVE
                else ''
            )
            common.console.print(
                f'campaign {campaign.name} #{campaign.id} already registered; config re-read from piloop.toml{resumed}'
            )
    if documents.added or documents.unchanged or documents.skipped:
        common.console.print(documents.line(), markup=False, highlight=False, soft_wrap=True)


FOLLOWED = (*MeasurementRecorded.kinds(), *CampaignPausedChanged.kinds(), *CampaignStatusChanged.kinds())
"""What ``init`` follows on the feed after it registers a campaign: the baseline's measurement, the pause that a
failed one makes, or a stop, after which the daemon measures nothing unasked."""


def _measured(client: Client, added: Registered) -> dict[str, float]:
    """Follow the feed after the registration until the daemon measures the baseline's full panel, and return its metrics.

    Exit with the reason when the campaign is paused or stopped first: a baseline that fails to evaluate pauses it.
    """
    cursor = added.cursor
    while True:
        query = EventsQuery(after=cursor, campaign=added.campaign.name, kind=FOLLOWED, wait=MAX_POLL_SECONDS)
        batch = client.events(query)
        for event in batch.events:
            match EVENT.validate_python(event.payload):
                case MeasurementRecorded(candidate=added.baseline, error=None, rung=None, metrics=metrics):
                    return metrics
                case CampaignPausedChanged(paused_reason=str(reason)):
                    raise SystemExit(reason)
                case CampaignStatusChanged(new=CampaignStatus.STOPPED, stopped_reason=reason):
                    raise SystemExit(reason or f'campaign {added.campaign.name} stopped')
                case _:
                    pass  # a failed measurement: its pause follows
        cursor = batch.next


def fork(
    ref: CampaignRef,
    /,
    *,
    at: Annotated[
        str | None,
        cyclopts.Parameter(help="The version to start from, as the source's vcs names one. Default: its head."),
    ] = None,
    name: Annotated[str | None, cyclopts.Parameter(help="The new campaign's name. Default: <source>-<n>.")] = None,
) -> None:
    """Make a new campaign that starts from a version of another one, by default its head, in a clone of its checkout.

    The source may be in any state, retired too, and goes on as it is. The fork takes its stored config and shares the
    memory of its repo: its lessons, ideas and documents. It starts with no ideas of its own and an empty workspace.
    The daemon measures its starting version as its baseline.
    """
    try:
        named = None if name is None else checked_campaign_name(name)
    except ValueError as e:
        raise SystemExit(str(e)) from e
    with Client.connect(daemon_http()) as client:
        [source] = client.status(Selection(campaign=ref, cwd=cwd())).campaigns
        added = client.register_campaign(Fork(fork=str(source.id), at=at, name=named))
    campaign = added.campaign
    common.console.print(
        f'campaign {campaign.name} #{campaign.id} forked from {source.name} #{source.id}; baseline'
        f' candidate#{added.baseline}: the daemon measures it',
        markup=False,
    )


def sync_cmd(
    ref: CampaignRef,
    /,
    *,
    secret: Annotated[
        str | None,
        cyclopts.Parameter(help="The name of the server's secret that the host of the campaign's repository asks for."),
    ] = None,
    token_stdin: Annotated[
        bool,
        cyclopts.Parameter(
            help="Read a token that the repository's host accepts from stdin, for this sync alone: nothing keeps it.",
            negative=(),
        ),
    ] = False,
) -> None:
    """Read a campaign's piloop.toml again. A campaign that the server cloned first takes its repository's new versions.

    Its checkout moves forward to them; its candidates and head stay. New code comes in with
    `piloop candidate import`, or a fork with `piloop campaign fork --at`. A local checkout is read where it is:
    `piloop init <path>` does the same. For a private repository: `--secret`, or a token piped in with
    `--token-stdin`.
    """
    body = SyncBody(credential=_sync_credential(secret, token_stdin))
    with Client.connect(daemon_http()) as client:
        synced = client.sync_campaign(campaign_name(client, ref), body)
    campaign = synced.campaign
    common.console.print(f'campaign {campaign.name} #{campaign.id}: config re-read', markup=False)


def _sync_credential(secret: str | None, token_stdin: bool) -> Credential | None:
    """The credential of ``piloop campaign sync``: the server's secret ``secret``, or the token that stdin holds."""
    match secret, token_stdin:
        case None, False:
            return None
        case str(), False:
            return SecretCredential(secret=secret)
        case None, True:
            if sys.stdin.isatty():
                raise SystemExit('--token-stdin reads the token from stdin: pipe it in')
            if not (token := sys.stdin.read().strip()):
                raise SystemExit('--token-stdin read no token from stdin')
            return TokenCredential(token=SecretStr(token))
        case _:
            raise SystemExit('--secret and --token-stdin each give the credential: pass one of them')


def parent(
    ref: CampaignRef,
    parent_ref: Annotated[str | None, cyclopts.Parameter(help='The campaign to put it under.')] = None,
    /,
    *,
    clear: Annotated[bool, cyclopts.Parameter(help='Make the campaign a root.', negative='')] = False,
) -> None:
    """Put a campaign under another, or make it a root with --clear.

    A campaign has at most one parent, in the same piloop home, and never itself or one of its descendants.
    `piloop campaign tree` shows the tree.
    """
    if (parent_ref is None) is not clear:
        raise SystemExit('name the parent or --clear, not both' if clear else 'name the parent, or --clear')
    with local.refused(), local.session() as s:
        above = None if parent_ref is None else local.campaign(s, parent_ref)
        campaign = ops.set_parent(s, local.campaign(s, ref).id, None if above is None else above.id)
        s.commit()
        where = 'a root' if above is None else f'under {above.name} #{above.id}'
        common.console.print(f'{campaign.name} #{campaign.id} is {where}', markup=False)


def tree() -> None:
    """Print every campaign under its parent, with its type and its status."""
    with local.session() as s:
        for depth, campaign in campaign_tree(s):
            line = f'{"  " * depth}{campaign.name} #{campaign.id}  {campaign.type}  {campaign.status}'
            common.console.print(line, markup=False, highlight=False)
