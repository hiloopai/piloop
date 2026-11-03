"""Generate ``docs/snippets/generated/example-stop-after-bests.mdx``: the example plugin ``examples/plugins/stop-after-bests``."""

from scripts.generate import SNIPPETS
from scripts.generate.plugin_examples import snippet

PATH = SNIPPETS / 'example-stop-after-bests.mdx'


def generate() -> str:
    """Return the entry points and the code of the example."""
    return snippet('stop-after-bests', 'piloop_stop_after_bests/__init__.py')
