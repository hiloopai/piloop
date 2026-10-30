"""Load-test ``server.py`` and report each rung of the ladder, then the full benchmark, as JSON lines.

The server runs in its own process, as a real one would, and this process measures it from outside. Each batch of
``concurrency`` requests is one line to the server; each request's latency is its batch's wall time. A rung sends its
requests again until it has run for its minimum seconds, so a fast server is measured long enough to rise above the
noise. ``match`` is the fraction of the checked tokens that equal ``model.reference``: the quality guardrail. Any failure exits non-zero with
the reason on stderr, and piloop records a crash.
"""

import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from model import VOCAB, reference

ROOT = Path(__file__).parent
PROMPT_TOKENS = 8
RUNGS = (
    # rung, requests, concurrency, new tokens per request, check one request in this many, minimum seconds
    ('smoke', 2, 2, 8, 1, 0),
    ('load', 8, 4, 16, 4, 0.5),
    (None, 32, 8, 16, 4, 3),
)


def main() -> int:
    """Start the server, climb the rungs, and report one line for each."""
    server = subprocess.Popen(
        [sys.executable, 'server.py'], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        ask(server, [[0]], 1)  # warm up: loading the model is not serving
        for seed, (rung, requests, concurrency, new_tokens, check_every, min_seconds) in enumerate(RUNGS):
            rng = random.Random(seed)
            prompts = [[rng.randrange(VOCAB) for _ in range(PROMPT_TOKENS)] for _ in range(requests)]
            outputs: list[list[int]] = []
            latencies: list[float] = []
            busy, served = 0.0, 0
            while not served or busy < min_seconds:
                for i in range(0, requests, concurrency):
                    batch = prompts[i : i + concurrency]
                    start = time.perf_counter()
                    answers = ask(server, batch, new_tokens)
                    busy += (took := time.perf_counter() - start)
                    latencies += [took] * len(batch)
                    outputs += answers if not served else []
                served += requests * new_tokens
            checked = range(0, requests, check_every)
            same = sum(
                a == b for i in checked for a, b in zip(outputs[i], reference(prompts[i], new_tokens), strict=True)
            )
            metrics = {
                'tokens': served,
                'seconds': round(busy, 4),
                'match': round(same / (len(checked) * new_tokens), 4),
                'p50_ms': round(percentile(latencies, 0.50) * 1000, 2),
                'p95_ms': round(percentile(latencies, 0.95) * 1000, 2),
            }
            if rung:
                report({'rung': rung, 'metrics': metrics})
            else:
                report({'status': 'ok', 'score': served / busy, 'direction': 'higher', 'metrics': metrics})
    except ServerError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        server.kill()
    return 0


class ServerError(Exception):
    """The server died, or answered something other than one output of the right length for each prompt."""


def ask(server: subprocess.Popen[str], prompts: list[list[int]], new_tokens: int) -> list[list[int]]:
    """Send one batch and return the server's outputs."""
    assert server.stdin is not None and server.stdout is not None
    server.stdin.write(json.dumps({'prompts': prompts, 'max_new_tokens': new_tokens}) + '\n')
    server.stdin.flush()
    line = server.stdout.readline()
    if not line:
        raise ServerError(f'the server exited with code {server.wait()}')
    try:
        outputs = json.loads(line)['outputs']
    except (ValueError, KeyError, TypeError) as e:
        raise ServerError(f'the server answered {line[:200]!r}') from e
    if len(outputs) != len(prompts) or any(len(o) != new_tokens for o in outputs):
        raise ServerError(f'expected {len(prompts)} outputs of {new_tokens} tokens, got {line[:200]!r}')
    return outputs


def percentile(values: list[float], q: float) -> float:
    """The nearest-rank percentile ``q`` of ``values``."""
    ranked = sorted(values)
    return ranked[max(math.ceil(q * len(ranked)) - 1, 0)]


def report(result: dict[str, object]) -> None:
    """Append the result to ``PILOOP_RESULT_FILE`` when piloop runs this command, else print it."""
    line = json.dumps(result)
    if path := os.environ.get('PILOOP_RESULT_FILE'):
        with open(path, 'a') as f:
            f.write(line + '\n')
    else:
        print(line)


if __name__ == '__main__':
    raise SystemExit(main())
