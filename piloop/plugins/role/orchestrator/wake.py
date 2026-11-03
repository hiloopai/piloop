"""How the orchestrator's agents start: its wake-ups on each pass over a campaign, and the launch of each agent.

The daemon asks the role on each pass (``StartsItsAgents.request``), and launches its requested rows as it launches
every row. The orchestrator answers with its wake-ups (``triggers``): ``run_failed`` requests an agent, and ``news``
writes a note for the agent, which requests one when none is open (``Steer``). Its agent runs without a run, in a
checkout of the head, with a brief made from its row.
"""

from datetime import datetime
from typing import TYPE_CHECKING, assert_never

from sqlalchemy import select

from piloop.actor import Agent, System
from piloop.agents.roles import briefing, compose_brief, compose_resumed, render_sections, role_of
from piloop.campaign.campaigns import WORKSPACE, workspace_dir
from piloop.campaign.stop import timeout as agent_timeout
from piloop.daemon.loop import Soonest
from piloop.db.events import emit
from piloop.db.models import OPEN, AgentSession, AgentSessionState, Campaign, Comment, utcnow
from piloop.db.triggers import DAEMON_ACTOR
from piloop.ids import AgentSessionId, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.consumers import AGENT_SESSIONS
from piloop.plugins.campaign_type.kit.evaluation.measure import score
from piloop.plugins.campaign_type.kit.verdicts.judge import checkout_candidate
from piloop.plugins.contribution import DaemonHost
from piloop.plugins.role.contract import PUSHED, Continuity, Wake
from piloop.plugins.role.orchestrator.news import news
from piloop.plugins.role.orchestrator.passes import Fire, RolePass, Steer, role_pass
from piloop.plugins.role.orchestrator.run_failed import assigned, run_failed
from piloop.plugins.section.contract import Rendering
from piloop.plugins.stop_rule.contract import Stop

if TYPE_CHECKING:  # the role's module imports this one, to run it
    from piloop.plugins.role.orchestrator import OrchestratorRole


def request(role: OrchestratorRole, wake: Wake) -> datetime | None:
    """Request an agent for the failed runs, and write the news on the role's open row, as its ``triggers`` say.

    In a quiet phase of the campaign only ``news`` is asked (``OrchestratorRole.wakes_while``). While a request of the
    role waits, a failed run requests nothing more: the next agent gets it. The request comes before the note, so the
    note goes on the row that the request made. Returns when to ask again for the news, or None for the next event.
    """
    p = role_pass(wake.s, wake.campaign, wake.role, wake.now, wake.quiet)
    waits = p.last is not None and p.last.state is AgentSessionState.REQUESTED
    soonest = Soonest()
    fire: Fire | None = None
    steer: Steer | None = None
    for name in role.triggers:
        match name:
            case 'run_failed' if wake.quiet is None and not waits:
                fire = run_failed(p)
            case 'run_failed':
                pass
            case 'news':
                steer, due = news(p, role.quiet_seconds)
                soonest.add(due)
            case _:
                assert_never(name)
    if fire is not None:
        _fire(wake, fire)
    if steer is not None:
        _steer(wake, p, steer)
    return soonest.at


def _fire(wake: Wake, fire: Fire) -> None:
    """Insert the requested row of ``fire``, with its events."""
    s, campaign, name = wake.s, wake.campaign, wake.role
    row = AgentSession(kind=name, campaign_id=campaign.id, reason=fire.reason)
    s.add(row)
    s.flush()
    for event in fire.events:
        emit(s, event, TargetRef(TargetKind.AGENT_SESSION, row.id), System.DAEMON)
    wake.tracer.event('agent_session.requested', campaign=campaign.name, kind=name, reason=fire.reason, count=1)


def _steer(wake: Wake, p: RolePass, steer: Steer) -> None:
    """Write ``steer`` as a message to the role's newest open row, or to a row requested for it, and its events on it."""
    s = wake.s
    row = s.scalars(p.mine().where(AgentSession.state.in_(OPEN)).order_by(AgentSession.id.desc()).limit(1)).first()
    if row is None:
        row = AgentSession(kind=p.role, campaign_id=p.campaign.id, reason=steer.reason)
        s.add(row)
        s.flush()
        wake.tracer.event(
            'agent_session.requested', campaign=p.campaign.name, kind=p.role, reason=steer.reason, count=1
        )
    message = Comment(
        target=TargetRef(TargetKind.AGENT_SESSION, row.id),
        author=DAEMON_ACTOR,
        kind=CommentKind.MESSAGE,
        body=steer.text,
    )
    s.add(message)
    s.flush()
    for event in steer.events:
        emit(s, event, TargetRef(TargetKind.COMMENT, message.id), System.DAEMON)
    wake.tracer.event('agent_session.steered', campaign=p.campaign.name, agent_session=row.id, comment=message.id)


def place(agent_session_id: AgentSessionId) -> str:
    """The place of the agent under the daemon's home: its log directory, with its worktree in it."""
    return f'{AGENT_SESSIONS}/{agent_session_id}'


def launch(role: OrchestratorRole, host: DaemonHost, agent_session_id: AgentSessionId) -> None:
    """Run the agent of the requested row ``agent_session_id``, in a worktree at the head, with a brief made from its row.

    The brief shows the role's sections (``[plugins.role.orchestrator] sections``). Its ``context`` section ends with the
    agent's lines: the checkout, the workspace, the role's own (``OrchestratorRole.context``), then the notes on the row
    so far, which it gets here and not again. An agent that a failed run woke reads the failures in its sections
    (``Rendering.given``). Nothing that it commits is scored. In a worktree at the head, the commits are removed with
    the worktree when the agent ends.
    """
    controller = host.controller
    with host.db.session() as s:
        agent_session = s.get_one(AgentSession, agent_session_id)
        campaign = s.get_one(Campaign, agent_session.campaign_id)
        cfg = campaign.config
        brief_role = role_of(s, agent_session)
        timeout = agent_timeout(s, campaign, cfg, role.timeout, utcnow())
        if isinstance(timeout, Stop):  # requested again: the pass that stops the campaign closes it
            agent_session.state = AgentSessionState.REQUESTED
            s.commit()
            host.tracer.event('agent_session.not_started', agent_session=agent_session_id, reason=timeout.reason)
            return
        base = checkout_candidate(s, campaign)
        values = ', '.join(
            f'{t.primary.name}={"unmeasured" if (v := score(base, campaign.evaluator_id, t.primary)) is None else v}'
            for t in cfg.all_tracks
        )
        now = utcnow()
        given = tuple(assigned(s, agent_session_id))
        notes = s.scalars(
            select(Comment)
            .where(Comment.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id))
            .where(Comment.kind == PUSHED)
            .order_by(Comment.id)
        ).all()
        workspace = workspace_dir(host.home, campaign.id)
        lines = [
            f'- Your cwd is a detached checkout of candidate#{base.id}, the head ({values}): read and run the'
            ' code, try things. Nothing you commit here is scored or kept; it goes with the checkout when you end.',
            f'- Shared workspace for notes and artifacts to other agents: `./{WORKSPACE}/` (not committed). The files'
            ' of a done idea are archived, and removed from it after some days or when the disk is short; `piloop'
            f' restore {campaign.name} <idea id>|<workspace path>` writes them back.',
            *(['', '# Notes', *(f'{n.body}\n' for n in notes)] if notes else []),
            f'- Sweep started at: {now:%Y-%m-%d %H:%M:%S} (UTC; the sections below are as of then).',
        ]
        agent = Agent(role=agent_session.kind, session=agent_session_id)
        brief_of = briefing(brief_role, campaign, agent, reason=agent_session.reason or 'requested', lines=lines)
        rendering = Rendering(s, now, campaign, workspace, controller.cas, given, brief=brief_of)
        sections = render_sections(cfg.sections_of(agent_session.kind), rendering)
        brief = compose_brief(brief_role, campaign, sections.whole)
        match role.continuity:
            case Continuity.RESUME:
                resumed = compose_resumed(role, agent, sections.resumed)
            case Continuity.FRESH:
                resumed = None
            case _:
                assert_never(role.continuity)
    artifact = controller.sources.agent(campaign, base.revision)  # outside the session: it puts the inputs in the CAS
    cas = controller.cas
    controller.launcher.launch(
        agent_session_id,
        brief=cas.put(brief.encode()),
        place=place(agent_session_id),
        artifact=artifact,
        timeout=timeout,
        resumed_brief=None if resumed is None else cas.put(resumed.encode()),
        read=[n.id for n in notes],
    )
