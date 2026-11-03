"""Generate ``docs/snippets/generated/example-best-cost.mdx``: the example plugin ``examples/plugins/best-cost``."""

from scripts.generate import SNIPPETS
from scripts.generate.plugin_examples import snippet

PATH = SNIPPETS / 'example-best-cost.mdx'


def generate() -> str:
    """Return the entry points and the code of the example."""
    return snippet('best-cost', 'piloop_best_cost/__init__.py')
