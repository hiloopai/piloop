"""What a package can add to piloop through entry points: triggers, roles, a role that starts its agents on a timer,
a runtime, a stop rule, a capability of its own with a provider and a best rule that calls it, and objects of the wrong
type."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Annotated, ClassVar

from piloop.api.views import Rendered
from piloop.db.models import EventModel
from piloop.ids import AgentSessionId
from piloop.plugins.campaign_type.kit.pool.best_rule.baseline import Baseline
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BestRule, Case
from piloop.plugins.contribution import DaemonHost
from piloop.plugins.fact_source.facts import ATTEMPTS, EVALUATIONS, FUNNEL, Fact, FactValue, since_best
from piloop.plugins.host import Capability, Parameters, Tier
from piloop.plugins.role.contract import ImplementerRole, Quiet, StartsItsAgents, Wake
from piloop.plugins.runtime.contract import Handle, RuntimeName
from piloop.plugins.runtime.subprocess import Subprocess, SubprocessRuntime
from piloop.plugins.section.contract import Rendering, Section, Surface
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

    SURFACE: ClassVar[Surface] = Surface.SWEEP
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


class Yields(Section):
    """A package's section called ``yields``, a name that piloop's own removed section had: it is its own now."""

    def render(self, r: Rendering) -> Rendered | None:
        return None


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
