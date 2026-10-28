"""``piloop watch`` is a client of the API: it imports the API's clients and models, never the database or the ops."""

import ast
from pathlib import Path

import piloop.watch

ALLOWED = ('piloop.api', 'piloop.ids', 'piloop.actor', 'piloop.text', 'piloop.settings', 'piloop.usage', 'piloop.watch')
"""What the watch may import: the API's client and models, the leaves that they use, and itself."""


def allowed(module: str) -> bool:
    """Whether ``module`` is in ``ALLOWED``, or a capability's or a component's client and models (``….wire``) or the
    typed ids that they carry (``….ids``).

    The lineage's models are in its contract (``piloop.plugins.lineage.contract``), beside its ``wire``.
    """
    return (
        not module.startswith('piloop.')
        or module.endswith(('.wire', '.ids'))
        or module == 'piloop.plugins.lineage.contract'
        or any(module == a or module.startswith(f'{a}.') for a in ALLOWED)
    )


def imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        match node:
            case ast.Import(names=names):
                found += [n.name for n in names]
            case ast.ImportFrom(module=str(module), level=0):
                found.append(module)
            case _:
                pass
    return found


def test_the_watch_imports_only_the_api_s_clients_and_models() -> None:
    package = Path(piloop.watch.__file__).parent
    leaks = {f'{p.name}: {m}' for p in sorted(package.glob('*.py')) for m in imports(p) if not allowed(m)}
    assert not leaks, 'the watch reads and writes through the API alone'


def test_the_guard_catches_what_opens_the_database() -> None:
    assert [allowed(m) for m in ('piloop.db.models', 'piloop.campaign.inbox', 'piloop.reports.overview')] == [
        False,
        False,
        False,
    ]
    assert allowed('piloop.plugins.campaign_type.kit.backlog.wire') and allowed('rich.text')
