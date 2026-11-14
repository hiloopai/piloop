"""How piloop says that a config does not load: one line for each bad key, with the fix.

It imports nothing of piloop, so the command line can catch a home whose settings do not load before it loads them.
"""

from pydantic import ValidationError


class HomeConfigError(ValueError):
    """A home's ``config.toml``, or a ``PILOOP_*`` variable, that does not load. The message names each key and fix."""


DOCS = 'https://piloop.autoresearch.inc/reference/configuration'
FIXES = {
    'extra_forbidden': f'delete the key or correct its spelling; {DOCS} lists every key',
    'missing': f'add the key; {DOCS} says what it takes',
}


def explain(where: object, error: ValidationError) -> str:
    """Pydantic's errors as one line per bad key: the key and value (``metrics[0].role = 'boss'``), problem and fix."""
    lines = [f'{where} is not a valid piloop config ({error.error_count()} problem(s)):']
    for e in error.errors():
        key = ''.join(f'[{part}]' if isinstance(part, int) else f'.{part}' for part in e['loc']).lstrip('.')
        value = '' if e['type'] == 'missing' or isinstance(e['input'], dict | list) else f' = {e["input"]!r}'
        message = e['msg'].removeprefix('Value error, ')
        fix = FIXES.get(e['type'], f'{DOCS} describes the key')
        lines.append(f'  {key or "(the file)"}{value}: {message}. Fix: {fix}')
    return '\n'.join(lines)
