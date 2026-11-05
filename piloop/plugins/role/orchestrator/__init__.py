"""``[plugins.role.orchestrator]``: piloop launches the campaign's orchestrator when news arrives for it.

Off by default: with ``triggers = []``, the person runs an orchestrator of their own (the ``piloop-operate`` skill), and
piloop launches none. ``triggers = ["news", "run_failed"]`` turns it on, for what needs a decision: an escalation,
what a person writes, a failed run (``news`` and ``run_failed``, two functions of this package). It never wakes on a
timer or a short board: it does not plan the work. Each agent of the role resumes the session of the one before
(``continuity = "resume"``). The news comes in notes on its row.
"""

from datetime import datetime
from importlib.resources import files
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field, model_validator

from piloop import deprecated
from piloop.ids import AgentSessionId
from piloop.plugins.role.contract import Continuity, Quiet, StartsItsAgents, Verb, Wake
from piloop.plugins.role.kit.guides import READER
from piloop.plugins.section.contract import SectionName, Surface

if TYPE_CHECKING:  # the daemon's types: the role's table loads with every config, without them
    from piloop.db.models import EventModel
    from piloop.plugins.contribution import DaemonHost

type Trigger = Literal['news', 'run_failed']
"""What may wake the orchestrator: an escalation or what a person writes (``news``), and a failed run (``run_failed``)."""
NEWS: Trigger = 'news'
LEAD_TRIGGERS: frozenset[str] = frozenset({NEWS, 'run_failed'})


class OrchestratorRole(StartsItsAgents):
    """``[plugins.role.orchestrator]``: decides escalations, answers messages, steers the work and records each decision.

    piloop starts its agents itself (``StartsItsAgents``): when one of its triggers wakes it (``wake``), and on request.
    """

    PROMPT = files(__package__) / 'prompts' / 'orchestrator.md'
    IDEAS: ClassVar[str] = (
        "- Add an idea: `{piloop} idea add {campaign} '<title>' --hypothesis '…' --plan '…' --from '<the evidence,"
        " with ids>'` (`--help` for links, a base, a priority)."
    )
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
            ('candidate', 'exclude'),
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

    triggers: list[Trigger] = []
    """Off by default. ``news``: an escalation, what a person writes, or the evaluation retry wake threshold. ``run_failed``: a failed run. No other. An
    empty list means that its agents start only on request (``piloop launch``)."""
    quiet_seconds: float = Field(default=30, ge=0)
    """How long the role waits after the first news to launch an agent, so that one agent takes a burst of it. An open
    agent gets the news at once."""
    continuity: Continuity = Continuity.RESUME
    timeout: float = 900
    max_concurrent: int = Field(default=1, ge=1, le=1)
    """One orchestrator at a time: two would answer the same news twice."""
    writes: list[str] = [
        'comments(decision) addressed to the human, with a priority',
        'answers to messages via inbox reply',
        'journal lines',
        'ideas, exclusions and the campaign changes of the piloop commands in this brief',
    ]
    GUIDES = (*READER, 'priorities', 'links', 'research_commands')
    sections: list[SectionName] | None = [
        *GUIDES,
        'context',
        'incidents',
        'headline',
        'trajectory',
        'runs',
        'queue',
        'agents',
        'candidates',
    ]
    """The guides, the failed runs that woke it (``incidents``), and the status that each brief shows after the news:
    lean, because each one stays in the resumed session."""
    SURFACE: ClassVar[Surface] = Surface.SWEEP

    @property
    def takes_escalations(self) -> bool:
        """Whether it wakes on ``news``, and so answers the agents' escalations before they reach a human."""
        return NEWS in self.triggers

    @classmethod
    def wakes_on(cls) -> frozenset[type[EventModel]]:
        """The inbox's events, for ``news``. ``run_failed`` reads the runs, which wake the pass anyway."""
        from piloop.plugins.role.orchestrator.news import WAKES  # the inbox and its models: no config loads them

        return WAKES

    def request(self, wake: Wake) -> datetime | None:
        from piloop.plugins.role.orchestrator.wake import request  # the daemon's machinery: no config loads it

        return request(self, wake)

    def wakes_while(self, quiet: Quiet) -> bool:
        """``news`` wakes it also while the campaign is paused, held or stopped: a person may write then."""
        return NEWS in self.triggers

    def starts_by_itself(self) -> bool:
        return bool(self.triggers)

    def launch(self, host: DaemonHost, agent_session_id: AgentSessionId) -> None:
        from piloop.plugins.role.orchestrator.wake import launch  # the daemon's machinery: no config loads it

        launch(self, host, agent_session_id)

    @model_validator(mode='before')
    @classmethod
    @deprecated.until('0.7.0', 'the lead files ideas with no cap in 0.6: delete _idea_caps')
    def _idea_caps(cls, data: object) -> object:
        """Drop ``files``, ``plan_words`` and ``max_ideas``, with a warning for each: of a file, or of a stored config."""
        if not isinstance(data, dict) or not (caps := [k for k in ('files', 'plan_words', 'max_ideas') if k in data]):
            return data
        for key in caps:
            deprecated.dropped(
                f'the lead files ideas with no cap now: delete {key} from [plugins.role.orchestrator]',
                removed_in='0.7.0',
            )
        return {k: v for k, v in data.items() if k not in caps}

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
