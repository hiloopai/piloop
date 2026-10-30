import shlex

from piloop.api.views import Rendered, Text
from piloop.plugins.section.contract import Rendering, Section, Surface


class Dev(Section):
    """The campaign's dev command (``[evaluator] dev``), which an implementer runs itself to check a change."""

    SHOWN_IN = frozenset({Surface.RUN})
    ORDER = 3

    def render(self, r: Rendering) -> Rendered | None:
        if (dev := r.campaign.config.evaluator.dev) is None:
            return None
        text = (
            f'Run `{shlex.join(dev)}` in your checkout to check a change, as often as you like. It is fast, and'
            ' piloop never runs it or records what it prints: only the evaluate command scores a version.'
        )
        return Rendered(title='Check a change', blocks=(Text(text=text),))
