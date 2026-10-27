"""Generate ``docs/snippets/generated/example-gpu-hours.mdx``: the fact source of ``examples/plugins/inference``."""

from scripts.generate import SNIPPETS
from scripts.generate.plugin_examples import snippet

PATH = SNIPPETS / 'example-gpu-hours.mdx'


def generate() -> str:
    """Return the entry point and the code of the fact source."""
    return snippet('inference', 'piloop_inference/gpu_hours.py', group='piloop.fact_sources')
