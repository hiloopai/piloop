"""Generate ``docs/snippets/generated/roles.mdx``: the built-in roles."""

from piloop.plugins.role.contract import ROLES, ImplementerRole
from piloop.plugins.role.orchestrator import OrchestratorRole
from scripts.generate import SNIPPETS, header
from scripts.generate.markdown import cell, lead

PATH = SNIPPETS / 'roles.mdx'


def generate() -> str:
    """Return a table of the built-in roles: whether each does runs, what starts its agents, what it is."""
    rows = ['| Role | Does runs | Starts on | What it is |', '|---|---|---|---|']
    for name, provider in ROLES.builtins().items():
        role = provider()
        if isinstance(role, OrchestratorRole):
            triggers = ', '.join(f'`{t}`' for t in role.triggers) or '`piloop launch` only: `triggers = []`'
        else:
            triggers = 'a free slot'
        what = cell(lead(type(role), type(role).__name__)).split(': ', 1)[-1]
        what = what[:1].upper() + what[1:]
        rows.append(f'| `{name}` | {"yes" if isinstance(role, ImplementerRole) else "no"} | {triggers} | {what} |')
    return (
        header('the defaults and docstrings of the built-in roles in `piloop/plugins/role/`')
        + '\n'
        + '\n'.join(rows)
        + '\n'
    )
