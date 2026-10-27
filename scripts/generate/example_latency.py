"""Generate ``docs/snippets/generated/example-latency.mdx``: the section of ``examples/plugins/inference``."""

from scripts.generate import SNIPPETS
from scripts.generate.plugin_examples import snippet

PATH = SNIPPETS / 'example-latency.mdx'


def generate() -> str:
    """Return the entry point and the code of the section."""
    return snippet('inference', 'piloop_inference/latency.py', group='piloop.sections')
