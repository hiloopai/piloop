"""Generate ``docs/snippets/generated/example-min-speedup.mdx``: the verdict rule of ``examples/plugins/inference``."""

from scripts.generate import SNIPPETS
from scripts.generate.plugin_examples import snippet

PATH = SNIPPETS / 'example-min-speedup.mdx'


def generate() -> str:
    """Return the entry point and the code of the verdict rule."""
    return snippet('inference', 'piloop_inference/min_speedup.py', group='piloop.verdict_rules')
