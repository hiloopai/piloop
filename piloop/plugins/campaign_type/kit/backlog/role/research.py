from importlib.resources import files

from pydantic import Field

from piloop.plugins.campaign_type.kit.backlog.trigger.low_water import LowWaterTrigger
from piloop.plugins.campaign_type.kit.runs.trigger.stalled import StalledTrigger
from piloop.plugins.harness.contract import HarnessKey
from piloop.plugins.role.contract import Files, Sweep, SweepRole
from piloop.plugins.role.kit.guides import READER
from piloop.plugins.trigger.contract import TriggerName


class ResearchRole(SweepRole):
    """``[plugins.role.research]``: research keeps the board stocked with ideas for the workers."""

    PROMPT = files(__package__) / 'prompts' / 'research.md'
    GUIDES = (*READER, 'search', 'past_campaigns', 'links')

    triggers: list[TriggerName] = ['low_water', 'stalled']
    """A short backlog, and a stall."""
    writes: list[str] = [
        'ideas via idea add, or the status of an open idea to dropped with a note',
        'comments(finding, escalation) on the campaign',
        'comments(note) on an idea',
    ]
    """What a research sweep may write, as its brief states it."""
    model: dict[HarnessKey, str] | None = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6.1-sol'}
    """The model for each harness in this role, over the harness's ``model``. A table that you set replaces
    this one."""
    files: Files = Files.IDEAS
    """Research adds ideas to the board."""
    timeout: float = 900
    """The number of seconds one sweep may run before it is stopped."""
    max_ideas: int = Field(default=3, ge=0)
    """The most new ideas that one sweep may insert, best first. 0 turns ``low_water`` off for research."""
    max_concurrent: int = Field(default=1, ge=1)
    """The maximum number of sweeps that run at the same time, each on the next harness of the rotation. The default
    is 1: sweeps that run at the same time read the same evidence and often duplicate each other."""

    def context(self, sweep: Sweep) -> list[str]:
        """This sweep's share of the backlog, the tracks short of their share, and the stall.

        The backlog's target has the ``buffer`` of the role's first ``low_water`` trigger, or none without one. The
        stall mark is that of its first ``stalled`` trigger: without one, the role has no stall.
        """
        triggers = sweep.campaign.config.triggers_of(sweep.role)
        buffer = next((t.buffer for t in triggers if isinstance(t, LowWaterTrigger)), 0)
        backlog = sweep.backlog(buffer)
        most = self.max_ideas
        mark = next((t.mark(sweep.campaign.config) for t in triggers if isinstance(t, StalledTrigger)), None)
        runs = sweep.without_keep(None)
        stalled = mark is not None and runs >= mark.every
        pivot = min(most, 1) if stalled else 0  # A stall gets its pivot whatever the backlog holds.
        own = max(min(backlog.short + most, most), pivot)  # This sweep's share of the shortfall.
        campaign = sweep.campaign
        pivots = ': stalled, so one of your own ideas pivots (section 3)' if pivot else ''
        short = ', '.join(
            f'{t.name} {t.gap} ({t.runnable} of {t.target} runnable)' for t in backlog.tracks if t.gap > 0
        )
        tracks = [
            f'- Tracks short of their share of the backlog: {short or "none"}. Give every idea the track it serves'
            ' (`--track`); fill the short ones first.'
        ]
        return [
            f'- Ideas to add: {own} (the board holds {backlog.held()}). Fewer only when what is left duplicates'
            ' open work; name that work in your finding.',
            *(tracks if campaign.config.multitrack else []),
            *(
                [
                    f'- Measured runs since the last keep: {runs}, of the {mark.every} that mark a stall ({mark.why}){pivots}.'
                ]
                if mark is not None
                else []
            ),
        ]
