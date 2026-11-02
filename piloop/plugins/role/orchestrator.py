"""``[plugins.role.orchestrator]``: piloop launches the campaign's orchestrator when news arrives for it.

Off by default: with ``triggers = []``, the person runs an orchestrator of their own (the ``piloop-operate`` skill), and
piloop launches none. ``triggers = ["news", "run_failed"]`` turns it on, for what needs a decision: an escalation,
what a person writes, a failed run. It never wakes on a timer or a short board: it does not plan the work. Each agent of the role resumes the session of
the one before (``continuity = "resume"``). The news comes in notes on its row (the ``news`` trigger).
"""

from importlib.resources import files
from typing import ClassVar

from pydantic import Field, model_validator

from piloop import deprecated
from piloop.plugins.role.contract import Continuity, Files, SweepRole, Verb
from piloop.plugins.role.kit.guides import READER
from piloop.plugins.section.contract import SectionName
from piloop.plugins.trigger.contract import TriggerName

LEAD_TRIGGERS = frozenset({'news', 'run_failed'})
"""What may wake the orchestrator: an escalation or what a person writes (``news``), and a failed run."""


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
            ('idea', 'status'),
            ('idea', 'link'),
            ('idea', 'unlink'),
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
        }
    )

    triggers: list[TriggerName] = []
    """Off by default. ``news``: an escalation, or what a person writes. ``run_failed``: a failed run. No other."""
    continuity: Continuity = Continuity.RESUME
    files: Files = Files.IDEAS
    timeout: float = 900
    max_concurrent: int = Field(default=1, ge=1, le=1)
    """One orchestrator at a time: two would answer the same news twice."""
    writes: list[str] = [
        'comments(decision) addressed to the human, with a priority',
        'answers to messages via inbox reply',
        'journal lines',
        'ideas, verdicts and the campaign changes of the piloop commands in this brief',
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

    @model_validator(mode='before')
    @classmethod
    @deprecated.until('0.7.0', 'the orchestrator of 0.5 woke on a timer: delete _planning_triggers, refuse the others')
    def _planning_triggers(cls, data: object) -> object:
        """Drop the triggers other than ``news`` and ``run_failed``, with a warning: of a file, or of a stored config."""
        triggers = data.get('triggers') if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(triggers, list):
            return data
        if not (others := [t for t in triggers if t not in LEAD_TRIGGERS]):
            return data
        deprecated.dropped(
            f'the orchestrator wakes only on news and run_failed now, not on {" or ".join(map(str, others))}: delete'
            ' them from [plugins.role.orchestrator] triggers',
            removed_in='0.7.0',
        )
        return {**data, 'triggers': [t for t in triggers if t in LEAD_TRIGGERS]}
