"""``[plugins.role.orchestrator]``: piloop launches the campaign's orchestrator when news arrives for it.

Off by default: with ``triggers = []``, the person runs an orchestrator of their own (the ``piloop-operate`` skill), and
piloop launches none. ``triggers = ["news"]`` turns it on. Each agent of the role resumes the session of
the one before (``continuity = "resume"``). The news comes in notes on its row (the ``news`` trigger).
"""

from importlib.resources import files
from typing import ClassVar

from pydantic import Field

from piloop.plugins.role.contract import Continuity, Files, SweepRole, Verb
from piloop.plugins.role.kit.guides import READER
from piloop.plugins.section.contract import SectionName
from piloop.plugins.trigger.contract import TriggerName


class OrchestratorRole(SweepRole):
    """``[plugins.role.orchestrator]``: decides escalations, answers messages, steers the work and records each decision."""

    PROMPT = files(__package__) / 'prompts' / 'orchestrator.md'
    RELAYED: ClassVar[frozenset[Verb]] = frozenset(
        {
            ('sql',),
            ('status',),
            ('history',),
            ('schema',),
            ('journal',),
            ('cas', 'get'),
            ('restore',),
            ('inbox',),
            ('inbox', 'show'),
            ('inbox', 'ack'),
            ('inbox', 'reply'),
            ('inbox', 'choose'),
            ('escalate',),
            ('idea', 'add'),
            ('idea', 'seed'),
            ('idea', 'develop'),
            ('idea', 'status'),
            ('idea', 'link'),
            ('idea', 'unlink'),
            ('idea', 'base'),
            ('idea', 'track'),
            ('candidate', 'verdict'),
            ('candidate', 'remeasure'),
            ('candidate', 'note'),
            ('candidate', 'import'),
            ('measurement', 'add'),
            ('eval', 'cancel'),
            ('prompt',),
            ('launch',),
            ('run', 'cancel'),
            ('agent', 'cancel'),
            ('pause',),
            ('resume',),
            ('campaign', 'stop'),
        }
    )

    triggers: list[TriggerName] = []
    """Off by default. ``["news"]``: an agent starts when news arrives for the orchestrator."""
    continuity: Continuity = Continuity.RESUME
    files: Files = Files.IDEAS
    timeout: float = 900
    max_concurrent: int = Field(default=1, ge=1, le=1)
    """One orchestrator at a time: two would answer the same news twice."""
    writes: list[str] = [
        'comments(decision) addressed to the human, with a priority',
        'answers to messages via inbox reply',
        'journal lines',
        'ideas, seeds, verdicts and the campaign changes of the piloop commands in this brief',
    ]
    GUIDES = (*READER, 'priorities', 'links', 'research_commands')
    sections: list[SectionName] | None = [
        *GUIDES,
        'context',
        'headline',
        'trajectory',
        'runs',
        'queue',
        'agents',
        'candidates',
    ]
    """The guides, and the status that each brief shows after the news: lean, because each one stays in the resumed
    session."""
