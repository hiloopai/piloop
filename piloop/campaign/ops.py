"""The control operations: the changes that the orchestrator or a human asks of a campaign, besides the daemon's own.

Each one is a typed function. It validates its input, makes the change and writes its event in the caller's session,
and the caller commits once. ``Refused`` says why an operation changed nothing. The CLI is a thin wrapper over these
functions, and any other client (an API, a remote controller) calls the same ones, so nothing here assumes the caller
is on the daemon's machine.

A change to a column that the change log covers (``piloop.db.triggers.changelog``) gets its event from the trigger,
signed by the connection's actor, so an operation writes no second event for it. A reason that no column holds goes
into a ``note`` on the row. An operation emits the events of the changes that no trigger logs.
"""

from collections.abc import Sequence
from typing import assert_never

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from piloop.actor import Actor
from piloop.campaign.stop import CampaignReopened, acted, check, request_stop, unclosed
from piloop.db.events import emit
from piloop.db.models import ENDED, AgentSession, Base, Campaign, Comment, Pause
from piloop.db.session import write_transaction
from piloop.ids import AgentSessionId, CampaignId, CampaignStatus, CommentKind, Ending, RoleName, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.runs.stop import settled
from piloop.plugins.stop_rule.contract import Go, Hold, Stop


class Refused(Exception):
    """An operation refused its input and changed nothing. The message says why."""


def _get[M: Base](s: Session, model: type[M], key: int, name: str) -> M:
    found = s.get(model, key)
    if found is None:
        raise Refused(f'no {name}#{key}')
    return found


def _reason(reason: str) -> str:
    if not reason.strip():
        raise Refused('a reason is required: say why, with ids')
    return reason.strip()


def pause(s: Session, campaign_id: CampaignId, *, evals: bool, reason: str) -> Campaign:
    """Pause the campaign: nothing launches, and with ``evals`` the daemon starts no measurement either.

    No run is queued, no role is requested and no requested row launches, so research files no ideas. A sweep that
    runs already files its ideas as ``proposed`` (``review_first``). Running work goes on and its commits are scored.
    Nothing is escalated. Without ``evals``, the daemon still measures what a new evaluator or a request needs, so the
    pause can end on a measured head. A pause is state in the database, so the daemon holds wherever the caller is.

    Raises:
        Refused: the reason is blank, or the campaign is paused that way already.
    """
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    why = _reason(reason)
    scope = Pause.ALL if evals else Pause.LAUNCHES
    if campaign.paused is scope:
        raise Refused(f'campaign {campaign.name} is paused already ({scope}): {campaign.paused_reason}')
    campaign.paused, campaign.paused_reason = scope, why
    return campaign


def resume(s: Session, campaign_id: CampaignId) -> Pause:
    """Resume a paused campaign, and return what it held. The daemon schedules it again at once.

    Raises:
        Refused: the campaign is not paused.
    """
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    if campaign.paused is None:
        raise Refused(f'campaign {campaign.name} is not paused')
    held = campaign.paused
    campaign.paused, campaign.paused_reason = None, None
    return held


def stop(s: Session, campaign_id: CampaignId, actor: Actor, *, reason: str, ending: Ending) -> Campaign:
    """Ask the daemon to stop the campaign at once (``request_stop``), as when a ``[stop]`` condition holds.

    On a stopped campaign, an ending ``now`` ends the implementers that still wind down.

    Raises:
        Refused: the campaign is stopped already, and the ending is not ``now``.
    """
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    if campaign.status is CampaignStatus.STOPPED and ending is not Ending.NOW:
        raise Refused(
            f'{campaign.name} is stopped already ({campaign.stopped_reason});'
            ' `piloop campaign stop --kill` ends its implementers at once'
        )
    request_stop(s, campaign, actor, reason=reason, ending=ending)
    return campaign


class StillStopping(Refused):
    """``retire`` or ``reopen`` refused a campaign whose stop is not over: ``agents`` are its open agents, by id."""

    def __init__(self, campaign: Campaign, agents: Sequence[AgentSessionId]) -> None:
        self.agents = tuple(agents)
        named = f'; agents {", ".join(f"#{i}" for i in agents)} are not over' if agents else ''
        super().__init__(
            f'{campaign.name} is still stopping{named}: try again once its closing review and runs are over'
        )


def _stopped(s: Session, campaign_id: CampaignId, verb: str) -> Campaign:
    """The stopped campaign ``campaign_id``, read under the write lock, once its stop is over: what ``verb`` changes.

    The write lock comes first, so no agent is requested or launched between this check and the caller's change: the
    session must have no open transaction.

    Raises:
        Refused: the campaign is active, or retired.
        StillStopping: the stop is not over: the closing review has not started, a run is not scored, or an agent of
            the campaign is open (requested, starting, running or ending).
    """
    write_transaction(s)
    s.expire_all()  # read inside the lock what an earlier read of this session may have cached
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    match campaign.status:
        case CampaignStatus.ACTIVE:
            raise Refused(
                f'{campaign.name} is active: only a stopped campaign {verb}s; stop it first, with'
                f' `piloop campaign stop {campaign.name}`'
            )
        case CampaignStatus.RETIRED:
            raise Refused(
                f'campaign #{campaign.id} ({campaign.name}) is retired, for good: fork it instead, with'
                f' `piloop campaign fork {campaign.id}`'
            )
        case CampaignStatus.STOPPED:
            pass
        case never:
            assert_never(never)
    open_ = s.scalars(
        select(AgentSession.id).where(AgentSession.campaign_id == campaign.id, AgentSession.state.not_in(ENDED))
    ).all()
    if unclosed(s, campaign) or not settled(s, campaign) or open_:
        raise StillStopping(campaign, open_)
    return campaign


def retire(s: Session, campaign_id: CampaignId, actor: Actor, *, reason: str) -> Campaign:
    """Retire a stopped campaign for good, once its stop is over: it changes no more, and another may take its name.

    Its rows, events and archives stay, and it is found by its id. Listings leave it out unless asked. The reason goes
    into a note on the campaign. A retired campaign never comes back: a fork of it is a new campaign.

    Raises:
        Refused: the reason is blank, or the campaign is not stopped (``_stopped``).
        StillStopping: the stop is not over.
    """
    why = _reason(reason)
    campaign = _stopped(s, campaign_id, 'retire')
    campaign.status = CampaignStatus.RETIRED
    target = TargetRef(TargetKind.CAMPAIGN, campaign.id)
    s.add(Comment(target=target, author=actor.signature, kind=CommentKind.NOTE, body=f'retired: {why}'))
    return campaign


def reopen(s: Session, campaign_id: CampaignId, actor: Actor, *, reason: str) -> Campaign:
    """Set a stopped campaign active again, once its stop is over, with the limits of its stop rule as they are.

    The runs, hours and tokens that the rule limits count from the reopen (``CampaignReopened``), so the campaign gets
    each of them again. A rule that would stop it again at once, a target that it reached say, refuses the reopen.

    Raises:
        Refused: the reason is blank, the campaign is not stopped (``_stopped``), or its stop rule stops it at once.
        StillStopping: the stop is not over.
    """
    why = _reason(reason)
    campaign = _stopped(s, campaign_id, 'reopen')
    campaign.status, campaign.stopped_reason = CampaignStatus.ACTIVE, None
    emit(s, CampaignReopened(reason=why), TargetRef(TargetKind.CAMPAIGN, campaign.id), actor)
    match acted(check(s, campaign, campaign.config)):
        case Stop(reason=stops):
            raise Refused(f'{campaign.name} would stop again at once ({stops}): change its stop rule first')
        case Go() | Hold():
            return campaign
        case never:
            assert_never(never)


def request_agent(
    s: Session, campaign_id: CampaignId, role: RoleName, *, harness: str | None, model: str | None, reason: str
) -> AgentSession:
    """Ask the daemon for one more agent of a sweeping role: it launches it at once, within ``max_concurrent``.

    Raises:
        Refused: the campaign has no sweeping role ``role``.
    """
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    roles = campaign.config.roles
    sweeping = roles.sweeping()
    if role not in sweeping:
        by_itself = (
            ' The daemon launches implementers by itself, one for each queued run.'
            if role in roles.doing_runs()
            else ''
        )
        raise Refused(f'{campaign.name} has no sweeping role {role!r}; it has {", ".join(sweeping)}.{by_itself}')
    agent = AgentSession(kind=role, campaign_id=campaign.id, harness=harness, model=model, reason=reason)
    s.add(agent)
    s.flush()
    return agent


def set_parent(s: Session, campaign_id: CampaignId, parent_id: CampaignId | None) -> Campaign:
    """Put the campaign under ``parent_id`` in the tree of campaigns, or make it a root with None, and return it.

    A campaign has at most one parent, a campaign of the same piloop home. The tree is for reading: it changes nothing
    about how either campaign runs. The ``campaigns_parent`` triggers refuse a cycle, whoever writes the column, and
    the ``campaign.parent_id`` event is the trigger's.

    Raises:
        Refused: a campaign is missing, the parent is the campaign's already, or the parent is the campaign itself or
            one of its descendants.
    """
    campaign = _get(s, Campaign, campaign_id, 'campaign')
    parent = None if parent_id is None else _get(s, Campaign, parent_id, 'campaign')
    if campaign.parent_id == parent_id:
        where = 'a root' if parent is None else f'under {parent.name}'
        raise Refused(f'{campaign.name} is {where} already')
    if parent is None:
        campaign.parent_id = None
        return campaign
    try:
        with s.begin_nested():
            campaign.parent_id = parent.id
    except IntegrityError as e:
        raise Refused(f'{campaign.name} cannot be under {parent.name}: {e.orig}') from e
    return campaign


def review_first(campaign: Campaign, actor: Actor, *, new: bool) -> str | None:
    """Return why an idea that ``actor`` adds (``new``) or develops waits for a review (``proposed``), or None.

    Only an agent of a sweeping role waits: while the campaign is paused, and for a new idea while its role's
    ``max_ideas`` is 0 (a sweep reads it once, in its brief; the operator may have set it to 0 since). The orchestrator
    and humans paused the campaign themselves.
    """
    if (agent := actor.launched) is None:
        return None
    name = agent.role
    role = campaign.config.roles.sweeping().get(name)
    if role is None:
        return None
    if campaign.paused is not None:
        return f'campaign {campaign.name} is paused'
    if new and role.max_ideas == 0:
        return f'[plugins.role.{name}] max_ideas = 0'
    return None
