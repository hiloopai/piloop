"""The inference server. Implementers edit this file.

It reads one batch of requests per line on stdin, ``{"prompts": [[3, 14, ...], ...], "max_new_tokens": 16}``, and
writes one line on stdout, ``{"outputs": [[...], ...]}``: for each prompt, the tokens that ``model.reference`` gives.
"""

import json
import math
import sys

from model import DECAY, DIM, VOCAB, E, U, W


def generate(prompt: list[int], max_new_tokens: int) -> list[int]:
    """Decode greedily, one token at a time, and recompute the whole context for each token."""
    tokens = list(prompt)
    for _ in range(max_new_tokens):
        context = [0.0] * DIM
        for distance, token in enumerate(reversed(tokens)):
            weight = DECAY**distance
            for j in range(DIM):
                context[j] += weight * math.tanh(sum(E[token][k] * W[k][j] for k in range(DIM)))
        logits = [sum(context[k] * U[k][v] for k in range(DIM)) for v in range(VOCAB)]
        tokens.append(max(range(VOCAB), key=logits.__getitem__))
    return tokens[len(prompt) :]


def main() -> None:
    """Serve batches until stdin closes."""
    for line in sys.stdin:
        batch = json.loads(line)
        outputs = [generate(p, batch['max_new_tokens']) for p in batch['prompts']]
        print(json.dumps({'outputs': outputs}), flush=True)


if __name__ == '__main__':
    main()
