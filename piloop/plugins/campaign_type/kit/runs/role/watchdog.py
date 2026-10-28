from importlib.resources import files

from pydantic import Field

from piloop.plugins.campaign_type.kit.runs.section.pack import Pack
from piloop.plugins.role.contract import SweepRole
from piloop.plugins.section.contract import SECTIONS, defaults
from piloop.plugins.trigger.contract import TriggerName


class WatchdogRole(SweepRole):
    """``[plugins.role.watchdog]``: the watchdog investigates the anomalies that the daemon detects and reviews progress."""

    PROMPT = files(__package__) / 'prompts' / 'watchdog.md'
    INCIDENT_PROMPT = files(__package__) / 'prompts' / 'watchdog_incident.md'
    ROUTINE_PROMPT = files(__package__) / 'prompts' / 'watchdog_progress.md'

    triggers: list[TriggerName] = ['run_failed', 'dead_at_startup', 'leaking_funnel', 'cron', 'stopped']
    """A failed run, an agent dead at startup or a leaking funnel requests an incident sweep, the timer requests a
    progress review when something is new, and the campaign's stop requests the closing review."""
    interval: float = Field(default=1800, ge=0)
    """The number of seconds between two progress reviews, counted from the last review. A sweep that investigates an
    anomaly is not a review. 0 turns the reviews off."""
    writes: list[str] = [
        'comments(escalation, finding, note)',
        'lessons on the campaign once it stopped',
        'seeds via idea seed',
    ]
    """What a watchdog may write, as its brief states it."""

    def shown(self) -> list[str]:
        """Its ``sections``, else a sweep's without the research pack: a review judges the work, it proposes none."""
        if self.sections is not None:
            return self.sections
        return [name for name in defaults(self.SURFACE) if SECTIONS.providers()[name].model is not Pack]
