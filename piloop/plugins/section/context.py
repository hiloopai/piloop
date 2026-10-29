from string import Formatter
from typing import ClassVar

from pydantic import field_validator

from piloop.agents.roles import PILOOP_HINT
from piloop.api.views import Rendered, Text
from piloop.plugins.section.contract import Rendering, Section, Surface, guides

FIELDS = ('agent', 'role', 'campaign_id', 'campaign', 'repo', 'reason', 'piloop', 'writes', 'guides')
"""What a line of the context may name: ``{agent}`` is its signature, ``{writes}`` what its role may write, and
``{guides}`` the guides that its brief leaves out."""


class Context(Section):
    """``[plugins.section.context]``: who the agent is, where its campaign is, why it started, and how it runs piloop.

    Then the lines that the campaign's type adds: the agent's run, or its sweep.
    """

    SHOWN_IN: ClassVar[frozenset[Surface]] = frozenset({Surface.SWEEP, Surface.RUN})
    ORDER: ClassVar[int] = 1

    lines: list[str] = [
        '- You are {agent}, the {role} for campaign#{campaign_id} ({campaign}).',
        "- Campaign repo: {repo}, on the daemon's machine. Your checkout can read every candidate's version.",
        "- Sign what you write as '{agent}' (comments.author, ideas.created_by).",
        '- Why this agent started (context, not a scope): {reason}',
        "- piloop: `{piloop}` is this daemon's piloop. Run every `piloop` command of this brief with it.",
        "- SQL tool: `{piloop} sql -`, with the statement on stdin from a quoted heredoc (`<<'EOF'`).",
        '- You may write: {writes}.',
        '- `{piloop} schema <guide>` prints another guide: {guides}.',
    ]
    """The lines, in order, each with fields in braces (``FIELDS``). A line with a field that is empty is left out."""

    @field_validator('lines')
    @classmethod
    def _known_fields(cls, lines: list[str]) -> list[str]:
        for line in lines:
            if unknown := [f for f in _fields(line) if f not in FIELDS]:
                raise ValueError(f'{line!r} has the field {unknown[0]}: use {", ".join(FIELDS)}')
        return lines

    def render(self, r: Rendering) -> Rendered | None:
        if (brief := r.brief) is None:
            return None
        campaign = r.campaign
        role = campaign.config.role(brief.agent.role)
        shown = role.shown()
        values = {
            'agent': brief.agent.signature,
            'role': brief.agent.role,
            'campaign_id': str(campaign.id),
            'campaign': campaign.name,
            'repo': campaign.path,
            'reason': brief.reason,
            'piloop': PILOOP_HINT,
            'writes': ', '.join(sorted(role.writes)),
            'guides': ', '.join(name for name in guides() if name not in shown),
        }
        lines = [line.format(**values) for line in self.lines if all(values[f] for f in _fields(line))]
        return Rendered(title='Context', blocks=(Text(text='\n'.join([*lines, *brief.lines])),))


def _fields(line: str) -> list[str]:
    return [field for _, field, _, _ in Formatter().parse(line) if field is not None]
