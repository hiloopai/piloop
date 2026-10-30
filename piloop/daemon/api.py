"""The API's routes: FastAPI path operations under ``/v1``, each calling the function that owns its answer.

``ROUTERS`` are the API, and ``with_api`` puts it on a server with what its routes read with. The routes share their
lookups as dependencies (``Selected``, ``Narrowed``) over one session per request. The OpenAPI document of the API
(``openapi``) is ``docs/reference/openapi.json``.
"""

import os
import time
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path as FilePath
from typing import Annotated, assert_never

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import ACTORS, Actor, Agent
from piloop.agents.results import PromptOutcome, queue_prompt
from piloop.api.contract import (
    GRANTS,
    Access,
    AckBody,
    CancelBody,
    Checkout,
    ChooseBody,
    Clone,
    CommentsQuery,
    Credential,
    EscalateBody,
    EventsQuery,
    Fork,
    GuideQuery,
    InboxQuery,
    LaunchBody,
    LimitBody,
    Located,
    MessageBody,
    NewsQuery,
    OutputQuery,
    PauseBody,
    Problem,
    PromptBody,
    Registration,
    ReopenBody,
    ReplyBody,
    RetireBody,
    SecretBody,
    SecretCredential,
    Selection,
    StopBody,
    SyncBody,
    TokenBody,
    TokenCredential,
)
from piloop.api.views import (
    Acked,
    AgentAnswer,
    AgentOutput,
    Batch,
    CampaignAnswer,
    CampaignOverview,
    CampaignSynced,
    Comments,
    DaemonState,
    Delivered,
    Delivery,
    Filed,
    Guide,
    Inbox,
    InvalidConfig,
    LiveKeys,
    Messaged,
    News,
    Overview,
    Pending,
    ProblemDetail,
    Prompted,
    Queued,
    Registered,
    Replied,
    SecretChanged,
    SecretList,
    SecretView,
    Status,
    Thread,
    TokenList,
    TokenMade,
    Undelivered,
)
from piloop.campaign import escalations, inbox as inbox_module, ops
from piloop.campaign.campaigns import (
    CampaignError,
    FromUrl,
    UnknownCampaign,
    cloned,
    fork,
    named,
    referenced,
    register,
    register_clone,
    selected,
    sync,
    updating_vcs,
    workspace_dir,
)
from piloop.campaign.stop import CANCELLABLE, request_cancel
from piloop.config import ConfigError
from piloop.db import feed as db_feed
from piloop.db.events import has, latest
from piloop.db.models import AgentSession, Campaign, Comment, Event, utcnow
from piloop.db.session import Database
from piloop.ids import (
    AgentSessionId,
    CampaignId,
    CampaignStatus,
    CommentId,
    CommentKind,
    EnvName,
    KeyOrigin,
    TargetKind,
    TargetRef,
)
from piloop.net import Refusal, tokens
from piloop.net.acting import MayNotAct, acts_as
from piloop.net.server import App, asked, authorized, changed
from piloop.net.tokens import Caller
from piloop.plugins import limits as limits_module
from piloop.plugins.campaign_type.kit.candidates.references import ImportRefused
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.measure import RemeasureRefused
from piloop.plugins.cas.contract import Cas
from piloop.plugins.contribution import collect, contributions_of, installed
from piloop.plugins.limits import live_keys
from piloop.plugins.memory.contract import MEMORY
from piloop.plugins.secrets import contract as secrets
from piloop.plugins.section.contract import Rendering, rendered
from piloop.plugins.vcs.contract import VCS, VcsError
from piloop.reports import feed, overview as reports_overview
from piloop.settings import Settings


@dataclass(frozen=True)
class Handlers:
    """What the routes read with: the daemon's database, settings and CAS, and the daemon itself."""

    db: Database
    cfg: Settings
    daemon: DaemonState
    cas: Cas


def handlers(request: Request) -> Handlers:
    """The ``Handlers`` that ``with_api`` put on the server's state."""
    found = request.app.state.handlers
    assert isinstance(found, Handlers)  # with_api sets them before the server serves
    return found


Api = Annotated[Handlers, Depends(handlers)]


def session(api: Api) -> Generator[Session]:
    """One session of the daemon's database for the request."""
    with api.db.session() as s:
        yield s


Reading = Annotated[Session, Depends(session)]


def campaigns_selected(s: Reading, query: Annotated[Selection, Query()]) -> list[Campaign]:
    """The campaigns that the query selects. Refuses a campaign that it names and the home does not have."""
    try:
        return selected(s, query.campaign, _cwd(query), query.status)
    except UnknownCampaign as e:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CAMPAIGN, str(e)) from e


def campaign_narrowed(s: Reading, query: Annotated[Located, Query()]) -> CampaignId | None:
    """The campaign that the query narrows to, or None for every campaign. Refuses one that the home does not have."""
    if query.campaign is None:
        return None
    try:
        return named(s, query.campaign, _cwd(query)).id
    except UnknownCampaign as e:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CAMPAIGN, str(e)) from e


Selected = Annotated[list[Campaign], Depends(campaigns_selected)]
Narrowed = Annotated[CampaignId | None, Depends(campaign_narrowed)]

reader = authorized(Access.READ)
"""The access of every route: one callable, so that FastAPI runs it once per request for each dependency on it."""


def acting(request: Request, caller: Annotated[Caller, Depends(reader)]) -> Actor:
    """Who the caller acts as: the actor that it asks for (``asked``), by the table of ``piloop.net.acting.acts_as``."""
    named = asked(request)
    try:
        actor = None if named is None else ACTORS.validate_python(named)
    except ValidationError as e:
        raise HTTPException(422, f'{named!r} is not an actor, such as orchestrator or implementer#12') from e
    try:
        return acts_as(caller, actor)
    except MayNotAct as e:
        raise Refusal(HTTPStatus.FORBIDDEN, Problem.FORBIDDEN, str(e)) from e


Acting = Annotated[Actor, Depends(acting)]
actor_access = authorized(Access.ACT)
"""The access of every route that writes."""
admin_access = authorized(Access.ADMIN)
"""The access of every route that reads or changes the home's own settings."""


def writing(api: Api, me: Acting) -> Generator[Session]:
    """One session of the daemon's database for the request, whose writes sign as the caller's actor."""
    with api.db.acting(me) as s:
        yield s


Writing = Annotated[Session, Depends(writing)]


def campaign_addressed(
    s: Writing, campaign: Annotated[str, Path(description="The campaign's name, or its id.")]
) -> Campaign:
    """The campaign that the path names, to change. Refuses one that the home does not have, and a retired one."""
    if (found := referenced(s, campaign)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CAMPAIGN, str(UnknownCampaign(campaign)))
    if found.status is CampaignStatus.RETIRED:
        raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'campaign #{found.id} ({found.name}) is retired')
    return found


def campaign_viewed(
    s: Reading, campaign: Annotated[str, Path(description="The campaign's name, or its id.")]
) -> Campaign:
    """The campaign that the path names, to read: a retired one too, by its id. Refuses one that the home does not have."""
    if (found := referenced(s, campaign)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CAMPAIGN, str(UnknownCampaign(campaign)))
    return found


def comment_addressed(s: Writing, comment: CommentId) -> Comment:
    """The comment that the path names. Refuses one that the home does not have."""
    if (found := s.get(Comment, comment)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_COMMENT, f'no comment#{comment}')
    return found


def agent_addressed(s: Writing, agent: AgentSessionId) -> AgentSession:
    """The agent that the path names. Refuses one that the home does not have."""
    return _agent(s, agent)


def _agent(s: Session, agent: AgentSessionId) -> AgentSession:
    if (found := s.get(AgentSession, agent)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_AGENT, f'no agent_session#{agent}')
    return found


Addressed = Annotated[Campaign, Depends(campaign_addressed)]
Viewed = Annotated[Campaign, Depends(campaign_viewed)]
AddressedAgent = Annotated[AgentSession, Depends(agent_addressed)]
Commented = Annotated[Comment, Depends(comment_addressed)]


REFUSALS_DOCUMENTED: dict[int | str, dict[str, object]] = {
    HTTPStatus.NOT_FOUND: {'model': ProblemDetail},
    HTTPStatus.CONFLICT: {'model': ProblemDetail},
    HTTPStatus.SERVICE_UNAVAILABLE: {'model': ProblemDetail},
}
"""What a route answers when it refuses: ``Refusal``'s 404 or 409, or 503 ``busy``, with a ``ProblemDetail``."""
router = APIRouter(
    prefix='/v1',
    dependencies=[Depends(reader)],
    responses=REFUSALS_DOCUMENTED,
)
"""Core's routes: the views, the feed, the inbox, campaigns, agents and the home's settings. Every route reads."""
ROUTERS = (router,)
"""The API: every router that ``with_api`` serves. A campaign type's routes are its contribution's."""


@router.get('/status', operation_id='status')
def status(api: Api, s: Reading, me: Acting, campaigns: Selected) -> Status:
    """The main facts of each campaign, at a cursor."""
    with db_feed.snapshot(s) as cursor:
        return Status(cursor=cursor, daemon=api.daemon, campaigns=feed.status_view(s, campaigns, me))


@router.get('/overview', operation_id='overview')
def overview(api: Api, s: Reading, campaigns: Selected) -> Overview:
    """The sections of each campaign's ``[plugins.section] use``, and the live keys that config.toml or a limit sets."""
    now = utcnow()
    shown: list[CampaignOverview | InvalidConfig] = []
    for c in campaigns:
        match feed.configured(s, c):
            case InvalidConfig() as invalid:
                shown.append(invalid)
            case cfg:
                rendering = Rendering(s, now, c, workspace_dir(api.cfg.home, c.id), api.cas)
                sections = rendered(cfg.status_sections(), rendering)
                shown.append(
                    CampaignOverview(id=c.id, name=c.name, path=c.path, parent=feed.parent_of(s, c), sections=sections)
                )
    return Overview(
        daemon=api.daemon, limits=live_keys(s, api.cfg.providers, (KeyOrigin.CONFIG, KeyOrigin.LIMIT)), campaigns=shown
    )


@router.get('/inbox', operation_id='inbox')
def inbox(s: Reading, me: Acting, campaign: Narrowed, query: Annotated[InboxQuery, Query()]) -> Inbox:
    """The inbox items that the caller did not write, and the seeds, at a cursor."""
    with db_feed.snapshot(s) as cursor:
        return feed.inbox_view(s, cursor, me, campaign_id=campaign, kinds=query.kind, acked=query.acked)


@router.get('/events', operation_id='events')
async def events(request: Request, api: Api, campaign: Narrowed, query: Annotated[EventsQuery, Query()]) -> Batch:
    """The events after a cursor. With wait, it answers once an event matches, or at the end of the wait with none.

    Each read runs in the thread pool, in a session of its own. Between two reads the route awaits the database's
    next wake and holds no thread. Each read starts at the cursor that the last one moved to, past what the filters
    left out.
    """
    deadline, seen = time.monotonic() + query.wait, 0
    batch = await run_in_threadpool(_events, api.db, query, campaign, query.after)
    while not (batch.events or batch.more) and (left := deadline - time.monotonic()) > 0:
        if (woke := await changed(request, api.db.changed, seen, left)) is None:
            break
        seen = woke
        batch = await run_in_threadpool(_events, api.db, query, campaign, batch.next)
    return batch


@router.get('/inbox/news', operation_id='news')
async def news(
    request: Request, api: Api, me: Acting, campaign: Narrowed, query: Annotated[NewsQuery, Query()]
) -> News:
    """What needs the orchestrator after a cursor, but what the caller itself wrote. With wait, a long-poll."""
    deadline, seen = time.monotonic() + query.wait, 0
    found = await run_in_threadpool(_news, api.db, campaign, me, query.after)
    while not found.items and (left := deadline - time.monotonic()) > 0:
        if (woke := await changed(request, api.db.changed, seen, left)) is None:
            break
        seen = woke
        found = await run_in_threadpool(_news, api.db, campaign, me, found.next)
    return found


@router.get('/inbox/{comment}', operation_id='thread')
def thread(s: Reading, comment: CommentId) -> Thread:
    """The thread of a comment, the same for each comment of it: the root in full, its replies, and its citations."""
    if (found := s.get(Comment, comment)) is None:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_COMMENT, f'no comment#{comment}')
    found_thread = inbox_module.thread_of(s, found)
    root = s.get_one(Comment, found_thread.comment.id)
    if root.kind != CommentKind.ESCALATION or root.form is None:  # free text shows as it was filed
        return found_thread
    item = escalations.escalated(s, root)
    return found_thread.model_copy(update={'escalation': escalations.policy(s, item.campaign).view(item)})


@router.post('/escalations', operation_id='escalate', dependencies=[Depends(actor_access)])
def escalate(s: Writing, me: Acting, body: EscalateBody) -> Filed:
    """File an escalation: the campaign's ``[plugins.escalation]`` provider checks its form, and says what to fix."""
    comment = escalations.escalate(s, body.target, body.form, author=me, priority=body.priority)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return Filed(cursor=cursor, comment=inbox_module.comment_view(comment))


@router.get('/escalations/guide', operation_id='escalation_guide')
def escalation_guide(s: Reading, query: Annotated[GuideQuery, Query()]) -> Guide:
    """How to fill the escalation form of the target's campaign: its provider's fields, limits and an example."""
    return Guide(text=escalations.policy(s, escalations.campaign_of_target(s, query.target)).guide())


@router.post('/inbox/{comment}/choose', operation_id='choose', dependencies=[Depends(actor_access)])
def choose(s: Writing, me: Acting, comment: Commented, body: ChooseBody) -> Replied:
    """Answer an escalation with one of its choices: a ``decision`` reply signed by the caller, and an ack."""
    answer = escalations.choose(s, comment.id, body.choice, author=me)
    with db_feed.snapshot(s) as cursor:
        return Replied(cursor=cursor, comment=inbox_module.comment_view(answer))


@router.post('/campaigns', operation_id='register_campaign', dependencies=[Depends(actor_access)])
def register_campaign(api: Api, s: Writing, me: Acting, body: Registration) -> Registered:
    """Register a campaign: a checkout where it is, a repository that the daemon clones under its home first, or a fork.

    Each registration of a URL clones it again, into a directory named after the new campaign's id. A fork clones the
    checkout of its source, a campaign in any state, and starts from a version of it: by default its head.

    It adds the campaign with its head version as the baseline candidate, and syncs its documents into its memory. The
    daemon then measures the baseline: its ``measurement.recorded`` event comes after the answer's cursor, and a
    baseline that fails pauses the campaign. A clone that does not register is deleted.
    """
    campaign, baseline = _registered(api, s, me, body)
    documents = MEMORY.selected(campaign.config.plugins).sync(s, campaign, me)
    s.flush()
    cursor = db_feed.newest(s)  # read in the write: no event of the baseline's measurement can come before it
    view = feed.status_view(s, [campaign], me)[0]
    s.commit()
    return Registered(cursor=cursor, campaign=view, baseline=baseline.id, documents=documents)


def _registered(api: Handlers, s: Session, me: Actor, body: Registration) -> tuple[Campaign, Candidate]:
    match body:
        case Checkout():
            return register(s, FilePath(body.path), me, body.name)
        case Clone(clone=remote):
            vcs, credential = VCS.selected(api.cfg.providers), _credential(api, remote.credential)
            return register_clone(s, api.cfg.home, vcs, FromUrl(remote.url, credential), me, body.name)
        case Fork():
            if (source := referenced(s, body.fork)) is None:
                raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_CAMPAIGN, str(UnknownCampaign(body.fork)))
            return fork(s, api.cfg.home, api.cas, source, me, at=body.at, name=body.name)
        case _:
            assert_never(body)


def _credential(api: Handlers, credential: Credential | None) -> SecretStr | None:
    """The value that ``credential`` gives: its token, or the home's secret that it names. Refuses an unknown secret."""
    match credential:
        case None:
            return None
        case TokenCredential(token=token):
            return token
        case SecretCredential(secret=name):
            if (value := secrets.HomeSecrets(api.db, _secrets(api)).get(name)) is None:
                raise Refusal(
                    HTTPStatus.CONFLICT, Problem.REFUSED, f'the home has no secret {name}: see `piloop secret list`'
                )
            return value
        case _:
            assert_never(credential)


@router.post('/campaigns/{campaign}/sync', operation_id='sync_campaign', dependencies=[Depends(actor_access)])
def sync_campaign(
    api: Api, s: Writing, me: Acting, campaign: Addressed, body: Annotated[SyncBody | None, Body()] = None
) -> CampaignSynced:
    """Read the campaign's ``piloop.toml`` again, and sync its documents into its memory.

    A campaign that the daemon cloned first takes what its repository has now: its new versions and tags, and its
    checkout moves forward to them. Its candidates and head stay: new code comes in as a reference candidate or a fork.
    A changed evaluate command, metric set or link makes a new evaluator. A stopped campaign whose stop conditions
    changed is active again.
    """
    match cloned(api.cfg.home, campaign), None if body is None else body.credential:
        case True, credential:
            with closing(updating_vcs(campaign).open(FilePath(campaign.path))) as repo:
                repo.update(_credential(api, credential))
        case False, None:
            pass
        case _:
            raise Refusal(
                HTTPStatus.CONFLICT,
                Problem.REFUSED,
                f'campaign {campaign.name} is a checkout of {campaign.path}, which the server did not clone:'
                ' a credential is for a cloned campaign, so sync it without one',
            )
    was = campaign.status
    sync(s, campaign)
    documents = MEMORY.selected(campaign.config.plugins).sync(s, campaign, me)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return CampaignSynced(
            cursor=cursor, campaign=feed.status_view(s, [campaign], me)[0], was=was, documents=documents
        )


@router.post('/campaigns/{campaign}/messages', operation_id='message', dependencies=[Depends(actor_access)])
def message(s: Writing, me: Acting, campaign: Addressed, body: MessageBody) -> Messaged:
    """Write to the campaign's orchestrator as the caller's actor: a ``message``, which wakes ``piloop inbox wait``."""
    sent = inbox_module.message(s, campaign.id, body.body, author=me, priority=body.priority)
    with db_feed.snapshot(s) as cursor:
        return Messaged(cursor=cursor, comment=inbox_module.comment_view(sent))


@router.post('/campaigns/{campaign}/pause', operation_id='pause', dependencies=[Depends(actor_access)])
def pause(s: Writing, me: Acting, campaign: Addressed, body: PauseBody) -> CampaignAnswer:
    """Pause a campaign: nothing new launches, and nothing is escalated. Running work goes on."""
    ops.pause(s, campaign.id, evals=body.evals, reason=body.reason)
    s.commit()
    return _campaign_answer(s, campaign, me)


@router.post('/campaigns/{campaign}/resume', operation_id='resume', dependencies=[Depends(actor_access)])
def resume(s: Writing, me: Acting, campaign: Addressed) -> CampaignAnswer:
    """Resume a paused campaign. The daemon queues runs and requests roles again at once."""
    ops.resume(s, campaign.id)
    s.commit()
    return _campaign_answer(s, campaign, me)


@router.post('/campaigns/{campaign}/stop', operation_id='stop', dependencies=[Depends(actor_access)])
def stop(s: Writing, me: Acting, campaign: Addressed, body: StopBody) -> CampaignAnswer:
    """Stop a campaign at once, as when a ``[stop]`` condition holds. The daemon ends its agents as ``ending`` says."""
    ops.stop(s, campaign.id, me, reason=body.reason, ending=body.ending)
    s.commit()
    return _campaign_answer(s, campaign, me)


@router.post('/campaigns/{campaign}/reopen', operation_id='reopen', dependencies=[Depends(actor_access)])
def reopen(s: Writing, me: Acting, campaign: Addressed, body: ReopenBody) -> CampaignAnswer:
    """Set a stopped campaign active again, once its stop is over.

    The runs, hours and tokens that its stop rule limits count again from now. A rule that would stop it at once
    refuses.
    """
    ops.reopen(s, campaign.id, me, reason=body.reason)
    s.commit()
    return _campaign_answer(s, campaign, me)


@router.post('/campaigns/{campaign}/retire', operation_id='retire', dependencies=[Depends(actor_access)])
def retire(s: Writing, me: Acting, campaign: Addressed, body: RetireBody) -> CampaignAnswer:
    """Retire a stopped campaign for good, once its stop is over.

    It changes no more, and another campaign may take its name. Its history stays, and its id still finds it.
    """
    ops.retire(s, campaign.id, me, reason=body.reason)
    s.commit()
    return _campaign_answer(s, campaign, me)


@router.get('/limits', operation_id='limits', dependencies=[Depends(admin_access)])
def limits(api: Api, s: Reading) -> LiveKeys:
    """Each live key: its value, and where it comes from (the default, ``config.toml``, or a limit)."""
    return _live_keys(api, s)


@router.put('/limits/{key}', operation_id='set_limit', dependencies=[Depends(admin_access)])
def set_limit(api: Api, s: Writing, me: Acting, key: str, body: LimitBody) -> LiveKeys:
    """Set a live key. It wins over ``config.toml`` at once, and holds until it is unset."""
    limits_module.set_limit(s, api.cfg.providers, key, body.value, reason=body.reason, actor=me.signature)
    s.commit()
    return _live_keys(api, s)


@router.delete('/limits/{key}', operation_id='unset_limit', dependencies=[Depends(admin_access)])
def unset_limit(api: Api, s: Writing, key: str) -> LiveKeys:
    """Remove a limit: ``config.toml`` holds again at once."""
    if not limits_module.unset_limit(s, key):
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_LIMIT, f'{key} has no limit: see `piloop limits`')
    s.commit()
    return _live_keys(api, s)


def _live_keys(api: Handlers, s: Session) -> LiveKeys:
    with db_feed.snapshot(s) as cursor:
        return LiveKeys(cursor=cursor, keys=live_keys(s, api.cfg.providers, tuple(KeyOrigin)))


@router.get('/tokens', operation_id='list_tokens', dependencies=[Depends(admin_access)])
def list_tokens(s: Reading) -> TokenList:
    """The bearer tokens: name, access and when each was made. Never the secrets."""
    return tokens.listing(s)


@router.post('/tokens', operation_id='create_token', dependencies=[Depends(admin_access)])
def create_token(s: Writing, body: TokenBody) -> TokenMade:
    """Make a token, and answer its secret: the only time that it is shown, since the home keeps its hash only."""
    made = tokens.made(s, body.name, body.access)
    s.commit()
    return made


@router.delete('/tokens/{name}', operation_id='revoke_token', dependencies=[Depends(admin_access)])
def revoke_token(s: Writing, name: str) -> TokenList:
    """Delete a token: the next call with it gets 401."""
    try:
        tokens.revoke(s, name)
    except tokens.NoToken as e:
        raise Refusal(HTTPStatus.NOT_FOUND, Problem.UNKNOWN_TOKEN, str(e)) from e
    s.commit()
    return tokens.listing(s)


@router.get('/secrets', operation_id='list_secrets', dependencies=[Depends(admin_access)])
def list_secrets(api: Api, s: Reading) -> SecretList:
    """The secrets: the name, whose it is, and who set it last and when. Never a value."""
    names = {row.id: row.name for row in s.execute(select(Campaign.id, Campaign.name))}
    return SecretList(
        secrets=[
            SecretView(
                name=found.secret.name,
                campaign=None if found.secret.owner is None else names[found.secret.owner],
                set_at=found.set_at,
                set_by=None if found.set_by is None else found.set_by.signature,
            )
            for found in secrets.listed(s, _secrets(api))
        ]
    )


def secret_owner(s: Reading, caller: Annotated[Caller, Depends(actor_access)], campaign: Narrowed) -> secrets.Owner:
    """Whose secret a write changes: the campaign named, with ``act``, else the home's, with ``admin``.

    A launched agent's token writes the secrets of its own campaign only.
    """
    match caller.identity, campaign:
        case Agent(session=session) as agent, owner if (
            owner is None or owner != s.get_one(AgentSession, session).campaign_id
        ):
            raise Refusal(
                HTTPStatus.FORBIDDEN, Problem.FORBIDDEN, f'{agent} writes only the secrets of its own campaign'
            )
        case _, None if Access.ADMIN not in GRANTS[caller.access]:
            raise HTTPException(
                HTTPStatus.FORBIDDEN, f"{caller.name} has {caller.access} access, and the home's secrets need admin"
            )
        case _:
            return campaign


SecretOwner = Annotated[secrets.Owner, Depends(secret_owner)]


@router.put('/secrets/{name}', operation_id='set_secret')
def set_secret(api: Api, s: Writing, me: Acting, name: EnvName, owner: SecretOwner, body: SecretBody) -> SecretChanged:
    """Set a secret of the home, or of the campaign named, in place of any. Agents get it from their next start.

    A campaign's secret needs ``act`` access, and the home's ``admin``. An agent sets only its own campaign's.
    """
    secrets.set_secret(s, _secrets(api), me, owner, name, body.value)
    s.commit()
    return _secret_changed(s, name, owner)


@router.delete('/secrets/{name}', operation_id='remove_secret')
def remove_secret(api: Api, s: Writing, me: Acting, name: EnvName, owner: SecretOwner) -> SecretChanged:
    """Delete a secret of the home, or of the campaign named. An agent that starts after it no longer gets it.

    A campaign's secret needs ``act`` access, and the home's ``admin``. An agent deletes only its own campaign's.
    """
    if not secrets.remove_secret(s, _secrets(api), me, owner, name):
        raise Refusal(
            HTTPStatus.NOT_FOUND,
            Problem.UNKNOWN_SECRET,
            f'{_whose(s, owner)} has no secret {name}: see `piloop secret list`',
        )
    s.commit()
    return _secret_changed(s, name, owner)


def _secrets(api: Handlers) -> secrets.SecretsProvider:
    return secrets.SECRETS.selected(api.cfg.providers)


def _secret_changed(s: Session, name: str, owner: secrets.Owner) -> SecretChanged:
    campaign = None if owner is None else s.get_one(Campaign, owner).name
    with db_feed.snapshot(s) as cursor:
        return SecretChanged(cursor=cursor, name=name, campaign=campaign)


def _whose(s: Session, owner: secrets.Owner) -> str:
    return 'the home' if owner is None else f'campaign {s.get_one(Campaign, owner).name}'


@router.post('/campaigns/{campaign}/agents', operation_id='launch', dependencies=[Depends(actor_access)])
def launch(s: Writing, campaign: Addressed, body: LaunchBody) -> AgentAnswer:
    """Ask the daemon for one more agent of a sweeping role: it launches it at once, within ``max_concurrent``."""
    agent = ops.request_agent(s, campaign.id, body.role, harness=body.harness, model=body.model, reason=body.reason)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return AgentAnswer(cursor=cursor, agent=agent.id, role=agent.kind)


@router.get('/agents/{agent}/output', operation_id='agent_output')
def agent_output(api: Api, s: Reading, agent: AgentSessionId, query: Annotated[OutputQuery, Query()]) -> AgentOutput:
    """The last lines that an agent wrote: the text of its latest session snapshot, else its runtime's output.

    The server reads them through the agent's harness, so a client needs to know no harness's files.
    ``agent_session.session_saved`` says that a new snapshot is in.
    """
    found = _agent(s, agent)
    return AgentOutput(agent=found.id, lines=reports_overview.agent_output(s, found, query.lines, api.cas))


@router.get('/comments', operation_id='comments')
def comments(s: Reading, query: Annotated[CommentsQuery, Query()]) -> Comments:
    """The comments on one row, such as an idea, oldest first."""
    on = select(Comment).where(Comment.target == TargetRef(query.kind, query.id)).order_by(Comment.id)
    with db_feed.snapshot(s) as cursor:
        return Comments(cursor=cursor, comments=[inbox_module.comment_view(c) for c in s.scalars(on)])


@router.post('/agents/{agent}/prompt', operation_id='prompt', dependencies=[Depends(actor_access)])
async def prompt(request: Request, api: Api, me: Acting, agent: AgentSessionId, body: PromptBody) -> Prompted:
    """Queue a message for the next turn of a running agent: a note on it, which the daemon delivers.

    With wait, it answers once something came of the note, or at the end of the wait with ``pending``. The note is
    written first, in the thread pool; between two reads of its outcome the route holds no thread.
    """
    comment = await run_in_threadpool(_queue_prompt, api.db, me, agent, body.text)
    deadline, seen = time.monotonic() + body.wait, 0
    answer = await run_in_threadpool(_prompted, api.db, agent, comment)
    while isinstance(answer.delivery, Pending) and (left := deadline - time.monotonic()) > 0:
        if (woke := await changed(request, api.db.changed, seen, left)) is None:
            break
        seen = woke
        answer = await run_in_threadpool(_prompted, api.db, agent, comment)
    return answer


def _queue_prompt(db: Database, me: Actor, agent: AgentSessionId, text: str) -> CommentId:
    with db.acting(me) as s:
        _agent(s, agent)
        if (comment := queue_prompt(s, agent, text, me.signature)) is None:
            raise Refusal(HTTPStatus.CONFLICT, Problem.REFUSED, f'agent_session#{agent} is not running')
        s.commit()
        return comment


def _prompted(db: Database, agent: AgentSessionId, comment: CommentId) -> Prompted:
    on_agent = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent)
    with db.session() as s, db_feed.snapshot(s) as cursor:
        found = latest(s, PromptOutcome, on_agent, has(PromptOutcome, comment=comment))
        return Prompted(cursor=cursor, comment=comment, delivery=_delivery(None if found is None else found[1]))


def _delivery(outcome: PromptOutcome | None) -> Delivery:
    match outcome:
        case None:
            return Pending()
        case PromptOutcome(kind='comment.queued'):
            return Queued()
        case PromptOutcome(kind='comment.delivered'):
            return Delivered()
        case _:
            return Undelivered(error=outcome.error)


@router.post('/agents/{agent}/cancel', operation_id='cancel_agent', dependencies=[Depends(actor_access)])
def cancel_agent(s: Writing, me: Acting, agent: AddressedAgent, body: CancelBody) -> AgentAnswer:
    """Cancel an agent of any role: a requested one never starts, a running one ends as ``ending`` says.

    For an implementer, this cancels its run. The daemon acts at once.
    """
    if agent.state not in CANCELLABLE:
        raise Refusal(
            HTTPStatus.CONFLICT, Problem.REFUSED, f'agent_session#{agent.id} is not requested, starting or running'
        )
    request_cancel(s, agent, me, ending=body.ending)
    s.commit()
    with db_feed.snapshot(s) as cursor:
        return AgentAnswer(cursor=cursor, agent=agent.id, role=agent.kind)


@router.post('/inbox/ack', operation_id='ack', dependencies=[Depends(actor_access)])
def ack(s: Writing, me: Acting, body: AckBody) -> Acked:
    """Acknowledge inbox items, to say that the caller has seen them. Nothing waits for an acknowledgement."""
    results = inbox_module.ack(s, body.ids, me)
    with db_feed.snapshot(s) as cursor:
        return Acked(cursor=cursor, results=results)


@router.post('/inbox/{comment}/reply', operation_id='reply', dependencies=[Depends(actor_access)])
def reply(s: Writing, me: Acting, comment: Commented, body: ReplyBody) -> Replied:
    """Answer an inbox item with a ``decision`` on the root of its thread, which is an inbox item of its own."""
    answer = inbox_module.reply(s, comment.id, body.body, author=me, priority=body.priority)
    with db_feed.snapshot(s) as cursor:
        return Replied(cursor=cursor, comment=inbox_module.comment_view(answer))


def _campaign_answer(s: Session, campaign: Campaign, me: Actor) -> CampaignAnswer:
    with db_feed.snapshot(s) as cursor:
        return CampaignAnswer(cursor=cursor, campaign=feed.status_view(s, [campaign], me)[0])


def _news(db: Database, campaign: CampaignId | None, me: Actor, after: int | None) -> News:
    with db.session() as s:
        return inbox_module.news(s, db_feed.newest(s) if after is None else after, campaign_id=campaign, party=me.party)


def _events(db: Database, query: EventsQuery, campaign: CampaignId | None, after: int) -> Batch:
    with db.session() as s:
        try:
            return feed.read(s, after, campaign_id=campaign, kinds=query.kind, limit=query.limit)
        except db_feed.CursorAhead as e:
            raise Refusal(HTTPStatus.CONFLICT, Problem.CURSOR_AHEAD, str(e)) from e


def with_api(server: App, api: Handlers) -> App:
    """``server`` with the API's routes, which read with ``api``. An operation's refusal answers 409, ``refused``.

    Raises:
        PluginError: A contributed route has the method and path of one of the API's.
    """
    for routes in ROUTERS:
        server.mount_router(routes)
    for refused in REFUSALS:
        server.add_exception_handler(refused, _refused)
    server.add_exception_handler(ops.StillStopping, _still_stopping)
    server.state.handlers = api
    return server


REFUSALS: tuple[type[Exception], ...] = (
    ops.Refused,
    escalations.Refused,
    ImportRefused,
    RemeasureRefused,
    limits_module.Refused,
    tokens.NameTaken,
    CampaignError,
    ConfigError,
    VcsError,
)
"""What an operation raises when it refuses its input and changes nothing: each answers 409, ``refused``."""


def _refused(_request: Request, e: Exception) -> Response:
    return JSONResponse({'error': Problem.REFUSED, 'detail': str(e)}, status_code=HTTPStatus.CONFLICT)


def _still_stopping(_request: Request, e: Exception) -> Response:
    return JSONResponse({'error': Problem.STILL_STOPPING, 'detail': str(e)}, status_code=HTTPStatus.CONFLICT)


def daemon_state(started: str, listen: str | None) -> DaemonState:
    """This process, as the daemon that answers."""
    return DaemonState(pid=os.getpid(), started=started, listen=listen)


def openapi() -> dict[str, object]:
    """The OpenAPI document of ``ROUTERS`` and of the installed contributions' routers (``installed``).

    It is what ``GET /v1/openapi.json`` answers, and ``docs/reference/openapi.json``.
    """
    described = FastAPI(title='piloop', version='1')
    for routes in (*ROUTERS, *collect(contributions_of(installed()), ()).routers):
        described.include_router(routes)
    return described.openapi()


def _cwd(query: Located) -> FilePath | None:
    return None if query.cwd is None else FilePath(query.cwd)
