"""Generate ``docs/snippets/generated/best-rule-case.mdx``: the JSON Schema of the case that a ``command`` rule reads."""

import json

from piloop.plugins.campaign_type.kit.pool.best_rule.command import CASE
from scripts.generate import SNIPPETS, header

PATH = SNIPPETS / 'best-rule-case.mdx'


def generate() -> str:
    """Return the JSON Schema of ``Case``, as pydantic generates it."""
    schema = json.dumps(CASE.json_schema(), indent=2)
    source = '`piloop/plugins/campaign_type/kit/pool/best_rule/contract.py`'
    return f'{header(source)}\n```json\n{schema}\n```\n'
