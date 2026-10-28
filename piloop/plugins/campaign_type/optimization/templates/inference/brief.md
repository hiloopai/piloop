# inference: serve a small language model faster

## The task

Make `server.py` generate more tokens per second under load, with the same output. `server.py` serves the model in
`model.py`: it reads a batch of prompts per line on stdin and writes their greedy completions on stdout.

## The rules

- Edit only `server.py`. Do not edit `evaluate.py`, `model.py`, `piloop.toml` or this file.
- For each prompt, `server.py` returns the tokens that `model.reference` gives. Do not hardcode outputs, and do not
  read the prompts or the outputs of `evaluate.py`.
- Use only the Python standard library.
- Measure with `python3 evaluate.py`. It prints one line for each rung, `smoke` and `load`, then one for the full
  benchmark:
  - `tokens_per_s`: `tokens` ÷ `seconds` of the full benchmark. Higher is better. This is the metric to improve.
  - `match`: the share of the checked tokens that equal `model.reference`. It must stay at least 0.98. Floating-point
    drift that changes a rare token is allowed; an approximation that changes many is not.
  - `p95_ms`: the 95th percentile latency of a request. It may grow by at most 25% per step.
- Commit with `git add server.py && git commit -m "<what you did and why>"`. Every commit is scored. If the idea did
  not work, commit anyway and say so in the message.
- Do not stop to ask questions. Decide and commit.

## Starting points

The baseline recomputes every token's feature and the whole context for each new token, one request at a time.
A token's feature depends only on the token, and the context can be updated in place.
