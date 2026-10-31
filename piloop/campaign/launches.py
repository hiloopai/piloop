"""Launching the agents that campaigns request: the kernel's share of the campaigns' pass."""

from collections import Counter
from collections.abc import Collection
from contextlib import closing
from pathlib import Path
from typing import assert_never

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from piloop.agents.refill import usage_of
from piloop.campaign.campaigns import vcs_of
from piloop.config import CampaignConfig
from piloop.db.models import LIVE, AgentSession, AgentSessionState, Campaign, utcnow
from piloop.ids import AgentSessionId, CampaignId, RoleName
from piloop.otel import Tracer
from piloop.plugins.harness.contract import Usage
from piloop.plugins.harness_choice.contract import HARNESS_CHOICES, HarnessCase, chosen
from piloop.plugins.role.contract import Continuity, RoleConfig
from piloop.plugins.vcs.contract import NotARepository


def choose_harness(
    cfg: CampaignConfig, role: RoleName, full: Collection[str], turn: int, resumes: str | None = None
) -> str | None:
    """The harness that the campaign's harness choice gives the next agent of ``role``, or None: every one is ``full``.

    ``turn`` is how many agents of the role came before, and ``resumes`` the harness that resumes its last session.
    """
    rotation = cfg.role_harnesses(role)
    models = cfg.role_models(role)
    case = HarnessCase(
        role=role,
        rotation=tuple(rotation),
        full=frozenset(full),
        turn=turn,
        resumes=resumes,
        models={h: models[h] for h in rotation if h in models},
    )
    return chosen(HARNESS_CHOICES.selected(cfg.plugins), case)


def earlier(s: Session, agent_session: AgentSession) -> int:
    """How many agents of the role of ``agent_session`` its campaign requested before it."""
    return (
        s.scalar(
            select(func.count())
            .select_from(AgentSession)
            .where(
                AgentSession.campaign_id == agent_session.campaign_id,
                AgentSession.kind == agent_session.kind,
                AgentSession.id < agent_session.id,
            )
        )
        or 0
    )


def resumed_on(
    s: Session,
    campaign_id: CampaignId,
    kind: str,
    table: RoleConfig,
    harnesses: Collection[str],
    before: AgentSessionId | None = None,
) -> str | None:
    """The harness on which the next agent of the role ``kind`` can resume its role's last saved session, or None.

    Only a role that resumes its sessions has one, and only while ``harnesses`` still has it. ``before``: the agent's
    own row, when it has one.
    """
    match table.continuity:
        case Continuity.RESUME:
            earlier = AgentSession.id < before if before is not None else true()
            return s.scalar(
                select(AgentSession.harness)
                .where(
                    AgentSession.campaign_id == campaign_id,
                    AgentSession.kind == kind,
                    earlier,
                    AgentSession.session_file.is_not(None),
                    AgentSession.harness.in_(harnesses),
                )
                .order_by(AgentSession.id.desc())
                .limit(1)
            )
        case Continuity.FRESH:
            return None
        case _:
            assert_never(table.continuity)


def remove_checkouts(agent_dir: Path, campaign: Campaign, tracer: Tracer) -> None:
    """Remove the checkouts under a run's or a sweep's directory. If the campaign's repo is gone, record an event."""
    try:
        repo = vcs_of(campaign).open(Path(campaign.path))
    except NotARepository:
        tracer.event('worktrees.orphaned', agent_dir=str(agent_dir), campaign_path=campaign.path)
        return
    with closing(repo):
        for checkout in repo.checkouts(agent_dir):
            repo.discard(checkout)


def close(s: Session, agent_session: AgentSession, error: str, tracer: Tracer) -> None:
    """Close an agent session row that the launcher does not close (after a stop or a reap), and read its usage.

    The usage comes from a scan of the session store. The agent can spend many tokens before it is killed. If the
    budget ignored them, a restarted campaign could go past ``max_tokens``. A requested row never started and has
    no session to read.
    """
    agent_session.state, agent_session.ended_at, agent_session.error = AgentSessionState.KILLED, utcnow(), error
    if agent_session.session_id is None:
        return
    found = usage_of(s, agent_session)
    if isinstance(found, Usage):
        agent_session.usage = found
    elif found is not None:
        tracer.event('agent_session.usage_unknown', agent_session=agent_session.id, error=str(found))


def full_harnesses(s: Session, campaign: Campaign, cfg: CampaignConfig) -> set[str]:
    """Return the harnesses at their ``max_concurrent`` cap, counting live agents and the implementers of queued runs.

    A queued run's harness was chosen when it was queued (``_enqueue``), so its requested implementer holds its place.
    A requested sweep gets its harness only when it is taken (``_take``).
    """
    if not (caps := cfg.harness_caps):
        return set()
    queued = (AgentSession.state == AgentSessionState.REQUESTED) & AgentSession.kind.in_(cfg.roles.doing_runs())
    on = select(AgentSession.harness).where(
        AgentSession.campaign_id == campaign.id,
        AgentSession.state.in_(LIVE) | queued,
        AgentSession.harness.in_(caps),
    )
    agents = Counter(s.scalars(on))
    return {harness for harness, cap in caps.items() if agents[harness] >= cap}
