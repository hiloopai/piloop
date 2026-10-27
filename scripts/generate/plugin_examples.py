"""The snippet of an example plugin in ``examples/plugins/``: its entry points, then its code, as they are."""

import tomllib

from scripts.generate import ROOT, header

EXAMPLES = ROOT / 'examples' / 'plugins'


def snippet(name: str, *files: str, group: str | None = None) -> str:
    """Return the entry point tables of ``examples/plugins/<name>/pyproject.toml``, then each of its ``files``.

    With ``group``, only the table of that entry point group: a package that adds several providers shows each apart.
    """
    root = EXAMPLES / name
    groups = tomllib.loads((root / 'pyproject.toml').read_text())['project']['entry-points']
    tables = '\n\n'.join(
        f'[project.entry-points."{g}"]\n' + '\n'.join(f'{n} = "{v}"' for n, v in eps.items())
        for g, eps in groups.items()
        if group in (None, g)
    )
    blocks = [f'```toml pyproject.toml\n{tables}\n```']
    blocks += [f'```python {f}\n{(root / f).read_text().rstrip()}\n```' for f in files]
    return header(f'the example plugin in `examples/plugins/{name}/`') + '\n' + '\n\n'.join(blocks) + '\n'
