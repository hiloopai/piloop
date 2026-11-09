import shlex

from piloop.api.views import Rendered, Text
from piloop.plugins.section.contract import Rendering, Section, Surface

SUBMIT = (
    '- `piloop submit` scores the version that your checkout is at, on the full evaluation. It returns at once: work on.'
    " Its score comes as a `[piloop]` message, with its gain over its parent, your line's best and the best. A score"
    ' that beats the best is evaluated again until it is confirmed. Nothing is discarded: every score stays in the pool.'
)
"""The line that names ``piloop submit``: on a worker elsewhere, the relay lets an agent run it."""


class Measure(Section):
    """How an implementer measures its change: the dev command (``[evaluator] dev``), and which commits are scored."""

    SHOWN_IN = frozenset({Surface.RUN})
    ORDER = 3

    def render(self, r: Rendering) -> Rendered:
        cfg = r.campaign.config.evaluator
        lines = []
        if cfg.dev is not None:
            lines.append(
                f'- `{shlex.join(cfg.dev)}` checks a change fast, in your checkout: piloop never runs it or records it.'
            )
        lines += [
            SUBMIT,
            '- Submit each version that beats your best so far'
            + ('' if cfg.submissions is None else f': your run has {cfg.submissions} submissions')
            + '. Only submitted versions are scored.',
        ]
        return Rendered(title='Measure a change', blocks=(Text(text='\n'.join(lines)),))
