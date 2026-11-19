"""Generate ``docs/snippets/generated/example-paired-rule.mdx``: the example program ``examples/best-rules/paired.py``."""

from scripts.generate import ROOT, SNIPPETS, header

PATH = SNIPPETS / 'example-paired-rule.mdx'
SOURCE = ROOT / 'examples' / 'best-rules' / 'paired.py'


def generate() -> str:
    """Return the code of the example, as it is."""
    code = SOURCE.read_text().rstrip()
    return f'{header("the example program `examples/best-rules/paired.py`")}\n```python paired.py\n{code}\n```\n'
