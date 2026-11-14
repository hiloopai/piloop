"""What a package can add to piloop through entry points: triggers, roles, a role that starts its agents on a timer,
a runtime, a stop rule, a capability of its own with a provider and a best rule that calls it, and objects of the wrong
type."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Annotated, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.actor import Agent
from piloop.agents.roles import briefing, compose_brief, compose_resumed, render_sections, role_of
from piloop.campaign.campaigns import workspace_dir
from piloop.db.models import AgentSession, Campaign, Comment, EventModel, utcnow
from piloop.ids import AgentSessionId, TargetKind, TargetRef
from piloop.plugins.campaign_type.consumers import AGENT_SESSIONS
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BestRule, Case
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd
from piloop.plugins.campaign_type.kit.runs.tables import RunClaim
from piloop.plugins.campaign_type.optimization import Optimization
from piloop.plugins.contribution import DaemonHost
from piloop.plugins.fact_source.facts import ATTEMPTS, EVALUATIONS, FUNNEL, Fact, FactValue, since_best
from piloop.plugins.host import Capability, Parameters, Tier
from piloop.plugins.role.contract import PUSHED, Continuity, ImplementerRole, Quiet, StartsItsAgents, Wake
from piloop.plugins.runtime.contract import Handle, RuntimeName
from piloop.plugins.runtime.subprocess import Subprocess, SubprocessRuntime
from piloop.plugins.section.contract import Rendering, Surface
from piloop.plugins.stop_rule.contract import GO, Escalate, Go, Hold, Stop, StopCase, StopRule


class Typed(Subprocess):
    """``typed``: a background process, like ``subprocess``, under a name of its own."""


class Shown(Subprocess):
    """``shown``: a background process, like ``subprocess``, whose output is one line that names its pid."""

    def open(self, name: str) -> ShownRuntime:
        return ShownRuntime(name)


class ShownRuntime(SubprocessRuntime):
    def read(self, handle: Handle, lines: int = 80) -> str:
        return f'pane {handle.data["pid"]}'


class GoesOn(StopRule):
    """``goes_on``: a stop rule without limits, so it reads no fact and never holds or stops a campaign."""

    def reads(self) -> list[Fact[FactValue]]:
        return []

    def check(self, case: StopCase) -> Go | Hold | Stop:
        return GO


WATCHED: dict[Fact[FactValue], float] = {EVALUATIONS: 100, ATTEMPTS: 50, since_best('spend'): 300, FUNNEL: 1}


class Watches(StopRule):
    """``watches``: limits on the facts that ``limits`` does not limit, which it never acts on."""

    def reads(self) -> list[Fact[FactValue]]:
        return list(WATCHED)

    def check(self, case: StopCase) -> Go:
        return GO

    def describe(self, case: StopCase) -> list[str]:
        shown = {fact: case.facts.get(fact) for fact in WATCHED}
        return [
            f'{fact.name} {value if isinstance(value, int | float) else "—"}/{WATCHED[fact]:g}'
            for fact, value in shown.items()
        ]


class Escalates(StopRule):
    """``escalates``: a stop rule that always tells a human ``reason``, and lets the campaign go on."""

    reason: str

    def reads(self) -> list[Fact[FactValue]]:
        return []

    def check(self, case: StopCase) -> Escalate:
        return Escalate(self.reason)


class Quick(ImplementerRole):
    """``quick``: an implementer with a short timeout and one slot."""

    timeout: float = 60
    max_concurrent: int = 1


class Ticker(StartsItsAgents):
    """A role that wakes every ``interval`` seconds, as a watchdog of a package does: ``request`` returns its timer.

    Its table has keys that piloop's own removed roles had too (``interval``, ``triggers``, ``follow_head``): they are
    its own. It requests no agent, so it launches none.
    """

    SURFACE: ClassVar[Surface] = Surface.ORCHESTRATOR
    interval: float = 60
    triggers: list[str] = []
    follow_head: bool = False
    woken: ClassVar[list[tuple[str, datetime]]] = []
    """The campaign and the time of each ``request``, for the test that reads them."""

    @classmethod
    def wakes_on(cls) -> frozenset[type[EventModel]]:
        return frozenset()

    def request(self, wake: Wake) -> datetime | None:
        self.woken.append((wake.role, wake.now))
        return wake.now + timedelta(seconds=self.interval)

    def wakes_while(self, quiet: Quiet) -> bool:
        return False

    def starts_by_itself(self) -> bool:
        return True

    def launch(self, host: DaemonHost, agent_session_id: AgentSessionId) -> None:
        raise AssertionError('a ticker requests no agent')


class Auditor(StartsItsAgents):
    """``auditor``: a package's role whose agents start only on request (``piloop launch``), and do no runs.

    Each agent runs in a checkout of the campaign's best, with a brief of the role's sections and the notes written on
    its row before it started. With ``continuity = "resume"``, it continues the session of the role's last agent. Its
    rows launch in the quiet phases that ``awake`` names.
    """

    PROMPT: ClassVar[Traversable | None] = files(__package__) / 'auditor.md'
    SURFACE: ClassVar[Surface] = Surface.ORCHESTRATOR
    awake: list[Quiet] = []

    @classmethod
    def wakes_on(cls) -> frozenset[type[EventModel]]:
        return frozenset()

    def request(self, wake: Wake) -> datetime | None:
        return None

    def wakes_while(self, quiet: Quiet) -> bool:
        return quiet in self.awake

    def starts_by_itself(self) -> bool:
        return False

    def launch(self, host: DaemonHost, agent_session_id: AgentSessionId) -> None:
        controller = host.controller
        with host.db.session() as s:
            row = s.get_one(AgentSession, agent_session_id)
            campaign = s.get_one(Campaign, row.campaign_id)
            base = best_of(s, campaign)
            assert base is not None, 'an auditor runs on a campaign with a baseline'
            notes = s.scalars(
                select(Comment)
                .where(Comment.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id), Comment.kind == PUSHED)
                .order_by(Comment.id)
            ).all()
            agent = Agent(role=row.kind, session=agent_session_id)
            lines = [f'- Your cwd is a checkout of candidate#{base.id}, the best.', *(n.body for n in notes)]
            brief_of = briefing(role_of(s, row), campaign, agent, reason=row.reason or 'requested', lines=lines)
            r = Rendering(s, utcnow(), campaign, workspace_dir(host.home, campaign.id), controller.cas, brief=brief_of)
            sections = render_sections(campaign.config.sections_of(row.kind), r)
            brief = compose_brief(role_of(s, row), campaign, sections.whole)
            resumed = compose_resumed(self, agent, sections.resumed) if self.continuity is Continuity.RESUME else None
        cas = controller.cas
        controller.launcher.launch(
            agent_session_id,
            brief=cas.put(brief.encode()),
            place=f'{AGENT_SESSIONS}/{agent_session_id}',
            artifact=controller.sources.agent(campaign, base.revision),
            timeout=self.timeout,
            resumed_brief=None if resumed is None else cas.put(resumed.encode()),
            read=[n.id for n in notes],
        )


class Fast(ImplementerRole):
    """A role whose default runtime is ``nice``, a runtime only once a test installs it: defaults are not validated."""

    runtime: RuntimeName | None = 'nice'


class BestCheck(Parameters, ABC):
    """A capability that a plugin declares: a check that can refuse a candidate that ``baseline`` lets be best."""

    @abstractmethod
    def check(self, case: Case) -> str | None:
        """Return why the candidate may not be best, or None."""


BEST_CHECKS = Capability('best_check', BestCheck, tier=Tier.PURE)


class AsMeasured(BestCheck):
    """``as_measured``: refuses a candidate with fewer evaluations than the baseline has."""

    def check(self, case: Case) -> str | None:
        mine, theirs = case.candidate.measured, case.baseline.measured
        return f'{case.candidate.name} has {mine} evaluations, the baseline {theirs}' if mine < theirs else None


class Checked(Baseline):
    """``checked``: what ``baseline`` allows, but a candidate that one of ``vetoes`` refuses may not be best."""

    vetoes: list[Annotated[BestCheck, BEST_CHECKS.entry]]

    def refuse(self, case: Case) -> str | None:
        if (why := super().refuse(case)) is not None:
            return why
        held = [f'{BEST_CHECKS.name_of(c)}: {why}' for c in self.vetoes if (why := c.check(case)) is not None]
        return '; '.join(held) or None


class Unrefusing(BestRule):
    """A best rule without ``refuse``."""


NOT_A_ROLE = 'auditor'
NOT_A_RUNTIME = 'typed'


class AlsoDefault(Subprocess):
    """A runtime that claims to be the default as well: two defaults, which piloop refuses."""

    DEFAULT = True


class Needy(Subprocess):
    """A runtime with a setting that has no default: an instance only through a table that sets it."""

    level: int


class Waiting(Optimization):
    """``waiting``: optimization, except that a run's end leaves its idea claimed, for something else to settle."""

    def settle(self, s: Session, claim: RunClaim, end: RunEnd, submissions: Sequence[Candidate]) -> None:
        """Leave the claim: the work is done only when something else says so."""
