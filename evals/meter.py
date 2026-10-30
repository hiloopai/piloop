"""Count the tokens of every Claude Code and Codex session under the given homes, by harness and model.

It runs inside an arm's sandbox with only the standard library, and writes one JSON object to its last argument:
``{"claude": {model: tokens}, "codex": {model: tokens}}``, where ``tokens`` has ``input`` (not served by the cache),
``output``, ``cache_read``, ``cache_write`` and ``turns``. It reads the files the way piloop's harnesses do: a Claude
response counts once by its ``message.id``, across every file, since a resumed session copies earlier ones; a Codex
session is its last ``token_count`` total.

Usage: ``python3 meter.py <claude projects dir> <codex sessions dir> <out>``.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path

FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'turns')
Tokens = dict[str, int]


def records(path: Path) -> Iterator[dict[str, object]]:
    """The JSON objects of a JSONL file, skipping a line that a writer has not finished."""
    with path.open(encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def add(totals: dict[str, Tokens], model: str, tokens: Tokens) -> None:
    """Add ``tokens`` to the totals of ``model``."""
    mine = totals.setdefault(model, dict.fromkeys(FIELDS, 0))
    for k in FIELDS:
        mine[k] += tokens.get(k, 0)


def claude(root: Path) -> dict[str, Tokens]:
    """The tokens of each distinct response in the Claude Code session files under ``root``."""
    totals: dict[str, Tokens] = {}
    seen: set[object] = set()
    for path in sorted(root.rglob('*.jsonl')):
        for rec in records(path):
            message = rec.get('message')
            if rec.get('type') != 'assistant' or not isinstance(message, dict):
                continue
            usage = message.get('usage')
            if not isinstance(usage, dict) or message.get('id') in seen:
                continue
            seen.add(message.get('id'))
            tokens = {
                'input': usage.get('input_tokens') or 0,
                'output': usage.get('output_tokens') or 0,
                'cache_read': usage.get('cache_read_input_tokens') or 0,
                'cache_write': usage.get('cache_creation_input_tokens') or 0,
                'turns': 1,
            }
            add(totals, str(message.get('model') or 'unknown'), tokens)
    return totals


def codex(root: Path) -> dict[str, Tokens]:
    """The last running total of each Codex session file under ``root``, under the model its last turn named."""
    totals: dict[str, Tokens] = {}
    for path in sorted(root.rglob('*.jsonl')):
        model, last, turns = 'unknown', None, 0
        for rec in records(path):
            payload = rec.get('payload')
            if not isinstance(payload, dict):
                continue
            if rec.get('type') == 'turn_context' and isinstance(payload.get('model'), str):
                model = payload['model']
            info = payload.get('info')
            if rec.get('type') == 'event_msg' and payload.get('type') == 'token_count' and isinstance(info, dict):
                usage = info.get('total_token_usage')
                if isinstance(usage, dict):
                    last, turns = usage, turns + 1
        if last is not None:
            cached = last.get('cached_input_tokens') or 0
            tokens = {
                'input': (last.get('input_tokens') or 0) - cached,
                'output': last.get('output_tokens') or 0,
                'cache_read': cached,
                'cache_write': last.get('cache_write_input_tokens') or 0,
                'turns': turns,
            }
            add(totals, model, tokens)
    return totals


def main(claude_root: Path, codex_root: Path) -> dict[str, dict[str, Tokens]]:
    """The tokens of both harnesses."""
    return {'claude': claude(claude_root), 'codex': codex(codex_root)}


if __name__ == '__main__':
    Path(sys.argv[3]).write_text(json.dumps(main(Path(sys.argv[1]), Path(sys.argv[2]))))
