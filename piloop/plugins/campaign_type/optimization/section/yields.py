from piloop.api.views import Lines, Rendered
from piloop.plugins.campaign_type.optimization.trigger.low_yield import LowYield
from piloop.plugins.campaign_type.optimization.yields import recent, spell
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.text import age


class Yields(Section):
    """How far the latest finished runs got, in the window of the ``low_yield`` trigger, and whether it is low.

    A low yield names its escalation while that is open.
    """

    SHOWN_IN = frozenset({Surface.STATUS, Surface.SWEEP})
    ORDER = 176

    def render(self, r: Rendering) -> Rendered | None:
        trigger = LowYield.of(r.campaign.config)
        got = recent(r.s, r.campaign, trigger.window)
        if not got.runs:
            return None
        lines = [f'- {got}.']
        if (found := trigger.low(got)) is not None:
            raised = spell(r.s, r.campaign)
            escalated = (
                '' if raised is None else f' Escalated in comment#{raised[1].comment}, {age(raised[0].at, r.now)} ago.'
            )
            lines.append(f'- Low: {found.detail}.{escalated}')
        return Rendered(title='Yield', blocks=(Lines(lines=tuple(lines)),))
