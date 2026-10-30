from importlib.resources import files

from pydantic import Field

from piloop.plugins.campaign_type.optimization.section.pack import Pack
from piloop.plugins.role.contract import SweepRole
from piloop.plugins.role.kit.guides import READER
from piloop.plugins.section.contract import SECTIONS
from piloop.plugins.trigger.contract import TriggerName


class WatchdogRole(SweepRole):
    """``[plugins.role.watchdog]``: the watchdog investigates the anomalies that the daemon detects and reviews progress."""

    PROMPT = files(__package__) / 'prompts' / 'watchdog.md'
    INCIDENT_PROMPT = files(__package__) / 'prompts' / 'watchdog_incident.md'
    ROUTINE_PROMPT = files(__package__) / 'prompts' / 'watchdog_progress.md'
    GUIDES = (*READER, 'priorities', 'search', 'past_campaigns')

    triggers: list[TriggerName] = ['run_failed', 'dead_at_startup', 'low_yield', 'cron', 'stopped']
    """A failed run, an agent dead at startup or a low yield requests an incident sweep, the timer requests a
    progress review when something is new, and the campaign's stop requests the closing review."""
    interval: float = Field(default=1800, ge=0)
    """The number of seconds between two progress reviews, counted from the last review. A sweep that investigates an
    anomaly is not a review. 0 turns the reviews off."""
    writes: list[str] = [
        'comments(escalation, note)',
        'lessons on the campaign once it stopped',
    ]
    """What a watchdog may write, as its brief states it."""

    def shown(self) -> list[str]:
        """Its ``sections``, else a sweep's without the research pack: a review judges the work, it proposes none."""
        if self.sections is not None:
            return self.sections
        return [name for name in super().shown() if SECTIONS.providers()[name].model is not Pack]
