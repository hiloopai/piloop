"""A ``command`` fact source that reports ``spend`` reports ``billing_usd``, and the ``spend`` source sums it.

``spend`` is the sum of the facts of the ``spend`` source. A stored config that revision 0056 or the ``[spend]`` bridge
wrote has a ``command`` source that reports a fact of that name too. Each such source reports ``billing_usd`` instead:
its ``metrics``, its ``notices`` and the wrapper of its command. A source named ``spend`` is named ``billing``, as the
``[spend]`` bridge of ``piloop.deprecated`` names it. The ``spend`` source joins ``use`` and sums ``billing_usd``: a
``spend`` table that has no ``facts`` sums its default facts and ``billing_usd``.

The downgrade reports ``spend`` again and takes ``billing_usd`` out of the sum, and the ``spend`` table out of ``use``
when nothing is left in it. A source keeps the name ``billing``, and a wrapper of 0056, which printed a ``METRIC`` line,
is the result line that the code before this revision writes.

Revision ID: 0067
Revises: 0066
"""

import json
from collections.abc import Callable, Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0067'
down_revision: str | None = '0066'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here, as of this revision.
SPEND = 'spend'
BILLED = 'billing_usd'
BILLING = 'billing'
COMMAND = 'command'
DEFAULT_FACTS = ['compute_usd', 'models_usd']
"""The facts that a ``spend`` table without ``facts`` sums."""
DEFAULT_SOURCES = ['campaign', 'optimization']
"""The fact sources of a campaign that does not set ``use``."""


def _wrapper(fact: str) -> list[str]:
    """What runs a command that prints one number, so that it reports it as ``fact`` on the result line."""
    printed = f'out=$("$@") && printf \'{{"metrics": {{"{fact}": %s}}}}\\n\' "$out" >> "$PILOOP_RESULT_FILE"'
    return ['sh', '-c', printed, 'sh']


METRIC_WRAPPER = ['sh', '-c', 'out=$("$@") && printf "METRIC spend=%s\\n" "$out"', 'sh']
"""The wrapper that revision 0056 wrote."""
WRAPPERS = {
    SPEND: (_wrapper(SPEND), METRIC_WRAPPER),
    BILLED: (_wrapper(BILLED),),
}
"""The wrappers of a command that reports each fact. The first is the one that this revision, or the downgrade, writes."""


def upgrade() -> None:
    _rewrite(upgraded)


def downgrade() -> None:
    _rewrite(downgraded)


def _rewrite(change: Callable[[dict[str, object]], dict[str, object]]) -> None:
    bind = op.get_bind()
    for campaign_id, config in bind.execute(text('SELECT id, config FROM campaigns')).all():
        bind.execute(
            text('UPDATE campaigns SET config = :c WHERE id = :id'),
            {'c': json.dumps(change(json.loads(config))), 'id': campaign_id},
        )


def upgraded(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config in the shape of this revision."""
    if (sources := _sources(config)) is None or not (names := _reporting(sources, SPEND)):
        return config
    if SPEND in names:
        sources[BILLING] = sources.pop(SPEND)
        names = [BILLING if name == SPEND else name for name in names]
        match sources.get('use'):
            case list() as used:
                sources['use'] = [BILLING if name == SPEND else name for name in used]
            case _:
                pass
    for name in names:
        _report(sources[name], SPEND, BILLED)
    match sources.get('use', DEFAULT_SOURCES):
        case list() as used if SPEND not in used:
            sources['use'] = [*used, SPEND]
        case _:
            pass
    match sources.setdefault(SPEND, {'facts': []}):
        case {'facts': list() as facts} as table if BILLED not in facts:
            table['facts'] = [*facts, BILLED]
        case dict() as table if 'facts' not in table:
            table['facts'] = [*DEFAULT_FACTS, BILLED]
        case _:
            pass
    return config


def downgraded(config: dict[str, object]) -> dict[str, object]:
    """Return a stored config in the shape of the revision before."""
    if (sources := _sources(config)) is None:
        return config
    for name in _reporting(sources, BILLED):
        _report(sources[name], BILLED, SPEND)
    match sources.get(SPEND):
        case {'facts': list() as facts} as table if BILLED in facts:
            table['facts'] = [fact for fact in facts if fact != BILLED]
            if table == {'facts': []}:
                del sources[SPEND]
                match sources.get('use'):
                    case list() as used:
                        sources['use'] = [name for name in used if name != SPEND]
                    case _:
                        pass
        case _:
            pass
    return config


def _sources(config: dict[str, object]) -> dict[str, object] | None:
    """``plugins.fact_source`` of a stored config, or None."""
    match config.get('plugins'):
        case {'fact_source': dict() as sources}:
            return sources
        case _:
            return None


def _reporting(sources: dict[str, object], fact: str) -> list[str]:
    """The names of the ``command`` sources that report ``fact``. A source's provider is its name when it names none."""
    return [
        name
        for name, table in sources.items()
        if isinstance(table, dict) and table.get('provider', name) == COMMAND and fact in table.get('metrics', [])
    ]


def _report(source: dict[str, object], old: str, new: str) -> None:
    """Make ``source`` report ``new`` where it reported ``old``: its metrics, its notices and its command's wrapper."""
    match source.get('metrics'):
        case list() as metrics:
            source['metrics'] = [new if metric == old else metric for metric in metrics]
        case _:
            pass
    match source.get('notices'):
        case dict() as notices if old in notices:
            source['notices'] = {new if fact == old else fact: values for fact, values in notices.items()}
        case _:
            pass
    match source.get('command'):
        case list() as command:
            for wrapper in WRAPPERS[old]:
                if command[: len(wrapper)] == wrapper:
                    source['command'] = [*WRAPPERS[new][0], *command[len(wrapper) :]]
                    break
        case _:
            pass
