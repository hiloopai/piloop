"""Generate ``docs/reference/cli.mdx``: every command of piloop, from its help."""

import re

from piloop.cli.main import COMMANDS, app, load_for_help
from scripts.generate import ROOT, header
from scripts.generate.markdown import mdx

PATH = ROOT / 'docs' / 'reference' / 'cli.mdx'
SOURCE = (
    'the docstrings and parameter help of the commands in `piloop/cli/main.py` and the `piloop.commands` entry points'
)
HEAD = f"""---
title: CLI
description: Every piloop command and its options.
---

{header(SOURCE)}
Each command prints the same with `--help`. With `PILOOP_SERVER` set, a command that calls the API asks that server
([On a server](/guides/daemon#on-a-server)).
"""
COMMAND = re.compile(r'^### piloop (\S+)$', re.MULTILINE)


def generate() -> str:
    """Return the page: cyclopts' Markdown of each of piloop's own top-level commands, by name, safe for MDX."""
    load_for_help(['--help'])  # each command's module, for its docstring
    docs = app.generate_docs(heading_level=2).replace('```console\n', '```text\n').replace(' *[default: False]*', '')
    found = list(COMMAND.finditer(docs))
    ends = [m.start() for m in found[1:]] + [len(docs)]
    blocks = {m[1]: docs[m.start() : end].rstrip() for m, end in zip(found, ends, strict=True)}
    paths = COMMANDS.paths()
    own = {name: block for name, block in blocks.items() if paths.get(name, 'piloop.').startswith('piloop.')}
    return HEAD + '\n' + '\n\n'.join(_mdx(own[name]) for name in sorted(own)) + '\n'


def _mdx(block: str) -> str:
    """Escape ``{}<>`` outside the code blocks and code spans of ``block``."""
    parts = re.split(r'(^```.*?^```$)', block, flags=re.MULTILINE | re.DOTALL)
    return ''.join(part if i % 2 else mdx(part) for i, part in enumerate(parts))
