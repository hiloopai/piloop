# <campaign>: <one line that says what to improve and how it is measured>

Every agent reads this brief. Keep it under 400 words: what to improve, how to work, and the hard rules. When the
campaign changes, rewrite it to that size: never append a rule for each incident. `piloop.toml` holds the config, and
`evaluate.py` gives the score.

## Goal

The primary metric (`score`), its direction and the baseline. What "done" means. What must not get worse.

## How to work

- The files you may edit, and the files you must not (the evaluate command, data preparation, this brief).
- How to run the code and its tests. `uv run python evaluate.py` gives the score in about <minutes>.
- Save a scored version early, even when you expect a loss. The evaluator is the check: build no harness of your own.

## Hard rules

- What is not allowed, for example external data or a pretrained model.
- What needs the human's decision, such as a new dependency or a cost above a limit: ask with an escalation, and do
  not do it.

## Prior work

What was tried, and its score.
