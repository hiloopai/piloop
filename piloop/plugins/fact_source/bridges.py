"""The bridges of the fact_source capability: its keys of an older release, rewritten (``piloop.deprecated``).

This module imports no provider: the bridges run before any provider loads.
"""

from pathlib import Path

from piloop import deprecated
from piloop.version import Version

BILLING = 'billing'
"""The ``command`` source that a ``[spend] command`` is."""
BILLED = 'billing_usd'
"""The fact that ``BILLING`` reads, which is the ``spend`` that ``[spend]`` was."""
SPEND_COMMAND = (
    'sh',
    '-c',
    'out=$("$@") && printf \'{"metrics": {"billing_usd": %s}}\\n\' "$out" >> "$PILOOP_RESULT_FILE"',
    'sh',
)
"""What runs a ``[spend] command``, which prints one number, so that it reports ``{"metrics": {"billing_usd": …}}``."""
DEFAULT_SOURCES = ['campaign', 'optimization']
"""The fact sources of a campaign that does not set ``[plugins.fact_source] use``."""


@deprecated.bridge(
    '0.5.0',
    '[spend] is a command source and the facts of the spend source: delete spend_table, SPEND_COMMAND, BILLING,'
    ' BILLED and DEFAULT_SOURCES',
    after=('plugin_lists',),
)
def spend_table(toml: Path, raw: dict[str, object], removed_in: Version) -> None:
    """``[spend]`` is the ``command`` source ``billing``, which reads ``billing_usd``, and ``spend`` sums only that.

    So ``spend`` and ``hold_at = { spend = … }`` mean what they meant. The command prints one number, so it runs in
    ``SPEND_COMMAND``. ``notices`` are those of ``billing_usd``, and ``closing_review`` is ``while_held`` of
    ``[plugins.stop_rule]``. Both sources join ``[plugins.fact_source] use``, after its own sources. A table of either
    name that the file has wins. It runs after ``plugin_lists``, so a ``[[facts]]`` is a ``use`` already.
    """
    spend = raw.get('spend')
    if not isinstance(spend, dict):
        return
    del raw['spend']
    review = spend.pop('closing_review', None)
    command = spend.get('command')
    source: dict[str, object] = {
        'provider': 'command',
        'command': [*SPEND_COMMAND, *command] if isinstance(command, list) else command,
        'metrics': [BILLED],
    }
    if 'interval' in spend:
        source['interval'] = spend['interval']
    if 'notices' in spend:
        source['notices'] = {BILLED: spend['notices']}
    if (sources := deprecated.plugin_table(raw, 'fact_source')) is not None:
        match sources.get('use', DEFAULT_SOURCES):
            case list() as used:
                sources['use'] = [*used, *(name for name in (BILLING, 'spend') if name not in used)]
            case _:
                pass  # not a list: the validation refuses it
        sources.setdefault(BILLING, source)
        sources.setdefault('spend', {'facts': [BILLED]})
    if review is not None:
        deprecated.into(raw, 'stop_rule', {'while_held': review})
    deprecated.warn(
        toml,
        'spend',
        '[spend] is deprecated; write a [plugins.fact_source.<name>] source with provider = "command" that reports'
        ' <fact>, and name the fact in [plugins.fact_source.spend] facts',
        removed_in=removed_in,
    )
