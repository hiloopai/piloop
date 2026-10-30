"""The ``piloop idea`` commands, clients of the API: add an idea, and change its status, links, base or track."""

from typing import Annotated, Literal

import cyclopts

from piloop.api.contract import Reasoned
from piloop.cli import common
from piloop.cli.client import campaign_name, daemon_http
from piloop.cli.common import HARNESS_HELP, CampaignRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus, LinkKind
from piloop.plugins.campaign_type.kit.backlog.wire import BacklogClient, BaseBody, IdeaBody, StatusBody, TrackBody
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.evaluation.ids import TrackName

app = cyclopts.App(name='idea', help='Add an idea to the board, and change an idea: its status, links, base or track.')
PRIORITY_HELP = 'From 0 (urgent) to 4 (someday).'
BASE_HELP = 'The candidate id to start from instead of the head. This makes a branch.'
TRACK_HELP = 'The track it works on. Required when piloop.toml has more than one track.'
AFTER_HELP = 'An idea id to wait for. Repeatable. A worker can claim the idea once all are done or dropped.'
REASON_HELP = 'Why, with ids. It goes into the event or a note on the idea.'
IdeaArg = Annotated[IdeaId, cyclopts.Parameter(help='The idea.')]
Reason = Annotated[str, cyclopts.Parameter(help=REASON_HELP)]


@app.command(name='add')
def idea_add(
    ref: CampaignRef,
    title: Annotated[str, cyclopts.Parameter(help='A short title for the idea.')],
    /,
    *,
    hypothesis: Annotated[
        str, cyclopts.Parameter(help='Why this should help, and by how much. Default: the title.', show_default=False)
    ] = '',
    plan: Annotated[str | None, cyclopts.Parameter(help='The steps for the implementer.')] = None,
    from_: Annotated[
        str | None,
        cyclopts.Parameter(
            name='--from', help='What prompted the idea, with ids or a URL. Example: "finding comment#881: …".'
        ),
    ] = None,
    priority: Annotated[int, cyclopts.Parameter(help=PRIORITY_HELP)] = 2,
    harness: Annotated[str | None, cyclopts.Parameter(help=HARNESS_HELP)] = None,
    base: Annotated[CandidateId | None, cyclopts.Parameter(help=BASE_HELP)] = None,
    track: Annotated[TrackName | None, cyclopts.Parameter(help=TRACK_HELP)] = None,
    after: Annotated[tuple[IdeaId, ...], cyclopts.Parameter(help=AFTER_HELP)] = (),
) -> None:
    """Add an idea to the board. It is open.

    The `files` key of a role says whether its agents may add ideas. The orchestrator and humans may add ideas. The
    idea is signed as PILOOP_ACTOR. The idea and its links are written together, so the daemon never sees the idea
    without them.
    """
    body = IdeaBody(
        title=title,
        hypothesis=hypothesis,
        plan=plan,
        provenance=from_,
        priority=priority,
        harness=harness,
        base=base,
        track=track,
        after=after,
    )
    with BacklogClient.connect(daemon_http()) as client:
        idea = client.add_idea(campaign_name(client, ref), body).idea
    common.console.print(f'idea#{idea.id} {idea.status} p{idea.priority}: {idea.title}', markup=False)


@app.command(name='status')
def idea_status(
    idea_id: IdeaArg,
    status: Annotated[IdeaStatus, cyclopts.Parameter(help='open or dropped.')],
    /,
    *,
    reason: Reason,
) -> None:
    """Set an idea's status, with a note on the idea that says why.

    `open` puts it back on the board, and `dropped` takes it off, which frees the ideas after it. Only a run makes an
    idea `done`. A claimed idea has an open run: it can only be dropped, and its run finishes.
    """
    with BacklogClient.connect(daemon_http()) as client:
        was = client.set_status(idea_id, StatusBody(status=status, reason=reason)).was
    common.console.print(f'idea#{idea_id} {was} → {status}: {reason.strip()}', markup=False)


@app.command(name='link')
def idea_link(
    idea_id: IdeaArg,
    kind: Annotated[LinkKind, cyclopts.Parameter(help='after: wait until the target is done or dropped.')],
    target_id: Annotated[IdeaId, cyclopts.Parameter(help='The idea it links to.')],
    /,
    *,
    reason: Reason,
) -> None:
    """Link an idea that exists to another idea of its campaign: it waits until the target is done.

    The link is written in one transaction, so no worker claims the idea in between. A claimed idea takes no link: its
    run started already. A link that closes a cycle is refused.
    """
    with BacklogClient.connect(daemon_http()) as client:
        client.link(idea_id, kind, target_id, Reasoned(reason=reason))
    common.console.print(f'idea#{idea_id} {kind} idea#{target_id}', markup=False)


@app.command(name='unlink')
def idea_unlink(
    idea_id: IdeaArg,
    kind: Annotated[LinkKind, cyclopts.Parameter(help='after.')],
    target_id: Annotated[IdeaId, cyclopts.Parameter(help='The idea it links to.')],
    /,
    *,
    reason: Reason,
) -> None:
    """Delete an idea's link, for example an `after` link to a prerequisite that is still open, to free the idea."""
    with BacklogClient.connect(daemon_http()) as client:
        client.unlink(idea_id, kind, target_id, Reasoned(reason=reason))
    common.console.print(f'idea#{idea_id} no longer {kind} idea#{target_id}', markup=False)


@app.command(name='base')
def idea_base(
    idea_id: IdeaArg,
    base: Annotated[
        CandidateId | Literal['head'],
        cyclopts.Parameter(help='A candidate id to start from, or head: no pin, the head when the run starts.'),
    ],
    /,
    *,
    reason: Reason,
) -> None:
    """Set the candidate that an idea's runs start from, or clear the pin with `head`.

    A pin to another candidate than the head makes a branch. A run already open keeps its base; the next attempt
    takes the new one.
    """
    with BacklogClient.connect(daemon_http()) as client:
        client.set_base(idea_id, BaseBody(base=None if base == 'head' else base, reason=reason))
    start = 'the head' if base == 'head' else f'candidate#{base}'
    common.console.print(f'idea#{idea_id} starts from {start}', markup=False)


@app.command(name='track')
def idea_track(
    idea_id: IdeaArg,
    track: Annotated[TrackName, cyclopts.Parameter(help='A track of piloop.toml.')],
    /,
    *,
    reason: Reason,
) -> None:
    """Move an idea to another track of its campaign, with a note on the idea that says why.

    Its runs are judged on that track's primary. A scheduled idea keeps its track: its run is judged on it.
    """
    with BacklogClient.connect(daemon_http()) as client:
        was = client.set_track(idea_id, TrackBody(track=track, reason=reason)).was
    common.console.print(f'idea#{idea_id} track {was} → {track}', markup=False)
