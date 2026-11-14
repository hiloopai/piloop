from datetime import timedelta

from sqlalchemy import select

from piloop.agents.results import AgentEnded
from piloop.api.views import Lines, Rendered
from piloop.db.events import has
from piloop.db.models import LIVE, AgentSession, AgentSessionState, Event
from piloop.ids import AgentSessionId, TargetKind
from piloop.plugins.campaign_type.kit.backlog.ideas import yields
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.reports.overview import agent_output, on
from piloop.text import NONE, age, cell
from piloop.usage import human

STALLED_SHOWN = timedelta(hours=1)
"""How far back the section shows the agents that the liveness policy ended."""


class Agents(Section):
    """The agents: the live ones, those that the liveness policy ended lately, the count of each role, the tokens.

    A live agent shows its last line, and one that the liveness policy ended in the last hour why it ended. Then the
    yield of each source of ideas.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.ORCHESTRATOR})
    ORDER = 160

    def render(self, r: Rendering) -> Rendered:
        s, campaign, now = r.s, r.campaign, r.now
        cfg = campaign.config
        rows = s.scalars(
            select(AgentSession).where(AgentSession.campaign_id == campaign.id).order_by(AgentSession.id)
        ).all()
        live = [
            f'  - {w.kind}#{w.id} {w.harness or NONE}/{w.model or NONE} {age(w.started_at, now)}, on {on(s, w)}: '
            + (cell(output[-1]) if (output := agent_output(s, w, 1, r.cas)) else NONE)
            for w in rows
            if w.state in LIVE
        ]
        kinds = {w.id: w.kind for w in rows}
        ended = s.execute(
            select(Event.target_id.label('agent'), Event.at, Event.payload)
            .where(Event.target_kind == TargetKind.AGENT_SESSION, Event.target_id.in_(kinds))
            .where(has(AgentEnded, end=RunEnd.STALLED), Event.at >= now - STALLED_SHOWN)
            .order_by(Event.id)
        ).all()
        stalled = [
            f'  - {kinds[AgentSessionId(agent)]}#{agent} ended stalled: {payload.reason}, {age(at, now)} ago'
            for agent, at, payload in ended
            if isinstance(payload, AgentEnded)
        ]
        requested = [f'{w.kind}#{w.id}' for w in rows if w.state is AgentSessionState.REQUESTED]
        per_kind: list[str] = []
        spend: list[str] = []
        for kind in dict.fromkeys([*cfg.roles.all(), *(w.kind for w in rows)]):
            mine = [w for w in rows if w.kind == kind]
            killed = sum(w.state is AgentSessionState.KILLED for w in mine)
            never = sum(w.state is AgentSessionState.NEVER_WORKED for w in mine)
            ends = [f'{n} {what}' for n, what in ((killed, 'killed'), (never, 'never worked')) if n]
            per_kind.append(f'{len(mine)} {kind}' + (f' ({", ".join(ends)})' if ends else ''))
            spend.append(f'{kind} {human(sum(_tokens(w) for w in mine))}')
        hour = sum(_tokens(w) for w in rows if w.ended_at is not None and now - w.ended_at <= timedelta(hours=1))
        by_source = '; '.join(y.line() for y in yields(s, campaign.id)) or 'none yet'
        lines = (
            f'- Agents live: {len(live) or "none"}' + (f'; requested: {", ".join(requested)}' if requested else ''),
            *live,
            *(
                [f'- Ended stalled in the last hour (`[plugins.liveness]`): {len(stalled)}', *stalled]
                if stalled
                else []
            ),
            f'- Agents so far: {", ".join(per_kind)}.',
            f'- Ideas by source: {by_source}.',
            f'- Tokens: {human(sum(_tokens(w) for w in rows))} ({", ".join(spend)}); {human(hour)} by agents that'
            ' ended in the last hour.',
        )
        return Rendered(title='Agents', blocks=(Lines(lines=lines),))


def _tokens(w: AgentSession) -> int:
    return w.usage.total if w.usage else 0
