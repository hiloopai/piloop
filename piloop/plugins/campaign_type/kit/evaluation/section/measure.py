import shlex
from importlib.resources import files
from typing import assert_never

from piloop.api.views import Rendered, Text
from piloop.plugins.campaign_type.kit.evaluation.config import Scoring
from piloop.plugins.section.contract import Rendering, Section, Surface
from piloop.plugins.vcs.contract import VCS, prompted

SUBMIT = (
    '- `piloop submit` scores the version that your checkout is at, on the full evaluation, and waits for its verdict'
    ' and score: `keep` makes it the head, `discard` does not beat the head, `crash` did not run.'
)
"""The line that names ``piloop submit``: on a worker elsewhere, the relay lets an agent run it."""
RUN_END = files(__package__) / 'prompts' / 'run_end.md'
"""0.5's scoring: every version is scored after the run, and a version can ask to be noted or skipped."""


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
        match cfg.scoring:
            case Scoring.SUBMIT:
                lines += [
                    SUBMIT,
                    '- Submit each version that beats your best so far'
                    + ('' if cfg.submissions is None else f': your run has {cfg.submissions} submissions')
                    + '. Only submitted versions are scored and kept.',
                ]
            case Scoring.RUN_END:
                lines.append(prompted(RUN_END.read_text().strip(), VCS.selected(r.campaign.config.plugins)))
            case never:
                assert_never(never)
        return Rendered(title='Measure a change', blocks=(Text(text='\n'.join(lines)),))
