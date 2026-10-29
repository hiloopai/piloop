"""``piloop inbox ack``, ``reply`` and ``choose``, ``piloop message`` and ``piloop escalate``: answer, write, or ask."""

import json
import sys
from typing import Annotated

import cyclopts

from piloop.api.client import Client
from piloop.api.contract import AckBody, ChooseBody, EscalateBody, GuideQuery, InboxQuery, MessageBody, ReplyBody
from piloop.cli import common
from piloop.cli.client import campaign_name, cwd, daemon_http
from piloop.cli.common import CampaignRef
from piloop.ids import CommentId, CommentKind


def inbox_ack(
    *ids: Annotated[CommentId, cyclopts.Parameter(help='The ids of the items.')],
    up_to: Annotated[
        CommentId | None,
        cyclopts.Parameter(
            help='Acknowledge every unacknowledged item up to this id instead. --campaign and --kind narrow it.'
        ),
    ] = None,
    campaign: Annotated[str | None, cyclopts.Parameter(help='With --up-to: only the items of this campaign.')] = None,
    kind: Annotated[
        tuple[CommentKind, ...],
        cyclopts.Parameter(name='--kind', help='With --up-to: only the items of this kind. Repeatable.'),
    ] = (),
) -> None:
    """Acknowledge items to say that you have seen them.

    Nothing waits for an acknowledgement. To change a decision, write a `decision` comment on its target.
    """
    if bool(ids) == (up_to is not None):
        raise SystemExit('name the ids to acknowledge, or --up-to <id>')
    if ids and (campaign or kind):
        raise SystemExit('--campaign and --kind work only with --up-to <id>')
    with Client.connect(daemon_http()) as client:
        if up_to is not None:
            listed = client.inbox(InboxQuery(campaign=campaign, cwd=cwd(), kind=kind)).items
            ids = tuple(item.id for item in listed if item.id <= up_to)
        results = client.ack(AckBody(ids=ids)).results
    common.console.print('\n'.join(r.line() for r in results) or 'nothing to acknowledge', markup=False)


def inbox_reply(
    comment: Annotated[CommentId, cyclopts.Parameter(help='The id of the item.')],
    body: Annotated[str, cyclopts.Parameter(help='The answer.')],
    /,
    *,
    priority: Annotated[int, cyclopts.Parameter(help='From 0 (urgent) to 4 (someday).')] = 2,
) -> None:
    """Answer an item with a `decision` on the root of its thread, `in_reply_to` the item, signed as PILOOP_ACTOR.

    The answer is an inbox item of its own, so every orchestrator but you sees it in `piloop inbox wait`.
    """
    with Client.connect(daemon_http()) as client:
        answer = client.reply(comment, ReplyBody(body=body, priority=priority)).comment
    common.console.print(f'comment#{answer.id} decision on {answer.target}', markup=False)


def message(
    ref: CampaignRef,
    body: Annotated[str, cyclopts.Parameter(help='What to say.')],
    /,
    *,
    priority: Annotated[int, cyclopts.Parameter(help='From 0 (urgent) to 4 (someday).')] = 2,
) -> None:
    """Write to the campaign's orchestrator, signed as PILOOP_ACTOR. It ends the orchestrator's `piloop inbox wait`.

    The orchestrator answers with `piloop inbox reply`, and the answer is in your inbox.
    """
    with Client.connect(daemon_http()) as client:
        sent = client.message(campaign_name(client, ref), MessageBody(body=body, priority=priority)).comment
    common.console.print(f'comment#{sent.id} message on {sent.target}', markup=False)


def inbox_choose(
    comment: Annotated[CommentId, cyclopts.Parameter(help='The id of the escalation.')],
    choice: Annotated[str, cyclopts.Parameter(help='The id of one of its choices, such as A.')],
    /,
) -> None:
    """Answer an escalation with one of its choices: a `decision` reply signed as PILOOP_ACTOR, and an ack."""
    with Client.connect(daemon_http()) as client:
        answer = client.choose(comment, ChooseBody(choice=choice)).comment
    common.console.print(f'comment#{answer.id} decision on {answer.target}: {answer.body}', markup=False)


def escalate(
    target: Annotated[str, cyclopts.Parameter(help='What it is about, such as idea#12.')],
    /,
    *,
    priority: Annotated[int, cyclopts.Parameter(help='From 0 (urgent) to 4 (someday).')] = 2,
    guide: Annotated[bool, cyclopts.Parameter(help="Print how to fill the form of the target's campaign.")] = False,
) -> None:
    """Ask for a decision: an escalation on a target, with its form as JSON on stdin.

    The campaign's `[plugins.escalation]` provider checks the form and says what to fix. `--guide` prints its fields,
    limits and an example.
    """
    with Client.connect(daemon_http()) as client:
        if guide:
            common.console.print(
                client.escalation_guide(GuideQuery.model_validate({'target': target})).text, markup=False
            )
            return
        try:
            form = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            raise SystemExit(f'the form on stdin is not JSON: {e}') from e
        filed = client.escalate(
            EscalateBody.model_validate({'target': target, 'form': form, 'priority': priority})
        ).comment
    common.console.print(f'comment#{filed.id} escalation on {filed.target}', markup=False)
