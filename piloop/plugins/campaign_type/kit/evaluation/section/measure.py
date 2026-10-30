import shlex
from typing import assert_never

from piloop.api.views import Rendered, Text
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.section.contract import Rendering, Section, Surface

SUBMIT = (
    '- `piloop submit` scores the version that your checkout is at, on the full evaluation, and waits for its verdict'
    ' and score: `keep` makes it the head, `discard` does not beat the head, `crash` did not run.'
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
                f'- `{shlex.join(cfg.dev)}` checks a change fast. Run it in your checkout as often as you like: piloop'
                ' never runs it or records it.'
            )
        match cfg.scoring:
            case Scoring.SUBMIT:
                lines += [
                    SUBMIT,
                    f'- Your run has {cfg.submissions} submissions. A version that you do not submit is never scored.',
                ]
            case Scoring.RUN_END:
                lines.append(
                    '- After your run ends, piloop scores each of its versions on its own, and judges it. Nothing is'
                    ' scored while you work: do not wait for a score.'
                )
            case never:
                assert_never(never)
        return Rendered(title='Measure a change', blocks=(Text(text='\n'.join(lines)),))
