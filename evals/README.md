# evals

Runs piloop configurations and plain agent sessions on the same tasks, at the same $ and time budget, and scores
what each hands in on data that no agent saw.

- `tasks/<name>/`: `repo/` is the starting commit, with its brief and its dev evaluator. `task.toml` names the dev
  metric, the dev scores that count as 0 and 1, the dev data and the task's own `piloop.toml` keys. `sealed.py` is
  the sealed scorer; the data it reads lives outside this repo.
- `arms/<name>.toml`: a piloop install with its agents and extra `piloop.toml` keys, or one plain agent session.
- `python -m evals run <task> <arm>` runs one repeat, then scores it. `rescore <run dir>` scores a run again, and
  `report` writes one HTML page per task.

## How a run works

Every run gets its own Modal sandbox, from the same image: the task's repo at the same starting commit, its dev
data, both agent CLIs, and, for a piloop arm, that arm's piloop with a fresh home. The agents call models through
the gateway that `EVALS_GATEWAY` names, with `EVALS_GATEWAY_TOKEN`.

- **piloop arm:** `piloop init` in the repo, with the task's keys, the arm's agents and `max_hours`. An arm with
  `split = "sealed"` also gets the task's dev command as `[evaluator] dev`, and a sealed split of its own as
  `[evaluator] sealed`: `EVALS_SEALED_URI/<task>.campaign/`, copied into the campaign's home. It is not the holdout
  that scores every arm afterwards. Its sandbox gets `EVALS_WORKER_CPU` cores and `EVALS_WORKER_MEMORY_MB` for each
  worker (the runs it keeps open at once), and never less than the arm's `resources`. Before the campaign starts, its
  daemon gets `EVALS_EVALUATION_SLOTS_PER_WORKER` evaluation slots for each worker
  (`executor.pools.serve.slots.evaluation`), so that a large team's submissions do not queue. The run ends at
  the budget, at `max_hours`, or at the campaign's own stop; then the daemon gets `EVALS_STOP_GRACE_MINUTES` to
  score what was committed.
- **plain arm:** one Codex or Claude Code session, told to keep `main` at its best version. When it ends early, it
  is resumed with the same short prompt.

With `EVALS_PLATFORM=daytona`, a run splits by role:

- Each agent, a plain arm's included, runs in a Modal sandbox of 2 CPUs and a hard 8 GiB, whatever the arm asks.
- A piloop arm's daemon (`piloop serve --listen`, `piloop cas serve`) and its evaluators run on Daytona
  (`daytona.py`), one evaluation per sandbox, `EVALS_EVALUATORS_PER_WORKER` of them for each worker. So every timing
  that decides a verdict runs on one platform, with hard limits.
- The workers and the evaluators are `piloop worker`s in the pools `agents` and `eval`. They dial the daemon through
  Daytona's preview proxy, each with a token of its own.
- It needs `EVALS_DAYTONA_API_KEY`. The holdout's scorings run on Modal either way.

A run holds its agent sessions in `EVALS_SESSION_LEDGER` while it lives. The driver refuses a run that would take the
sessions of every run on this machine over `EVALS_MAX_SESSIONS`. Sessions that no driver here starts, such as another
campaign's on the same gateway pool, are a file there with `run`, `sessions` and `until`.

The driver reads the spend every `EVALS_POLL_SECONDS`: the tokens of every session file in the sandbox at list
price, plus the sandbox's CPU and memory. Each sample keeps the tokens too, by kind. The export (git bundle, piloop's database, session files, spend) goes to
`results/<task>/<arm>-<repeat>/`, and to `EVALS_ARCHIVE_URI` when it is set.

Scoring runs the base and every head of a run in sandboxes without network: the task's own `evaluate.py` over the
head's tree gives the dev score, and `sealed.py` the sealed one, with the data from `EVALS_SEALED_URI/<task>/`.
Heads are a piloop arm's keeps and a plain arm's commits on `main`. A task that times its runs scores each version in
`score_repeats` sandboxes and takes the means; `scores.json` keeps each scoring with its CPU. A task's `cloud` pins
the CPU type of its sandboxes, the agents' and the scorers'.

## Metrics

`result.json` holds, per run: the sealed and dev score of each head against hours, $, the models' $ and the tokens, $ and hours to the first
keep, the funnel (ideas, runs, submissions, kept), escalations, comments by people, the lead's share of the tokens,
and the gap between the dev and sealed score of the final head. Spend is split into models and compute. Before the agents
start, the driver evaluates the starting repo 5 times with the dev command and records the CPU (`selftest`): how much
the evaluator varies on that machine. Dev evaluations are counted where they run: `wrap/sitecustomize.py`, on the
sandbox's `PYTHONPATH`, logs each run of `evaluate.py`, however it was started. A piloop arm also records each role's
keeps, those of them that the final head contains, and its races: submissions that beat the version they started
from but not the head that another run had moved on to. The report draws each arm's median curve over repeats with
the band from its lowest to its highest repeat, with plotly, as static pages.

## Telemetry

Each step of the driver is a span: the run, its sandbox, each spend sample, the stop, the export, and the scoring of
each head. A span prints one line on the console when it ends. With `OTEL_EXPORTER_OTLP_ENDPOINT` (and
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_RESOURCE_ATTRIBUTES`) set, the spans go to that collector too, and a piloop arm's
daemon gets the same variables, with `evals.run` in its resource.

## Run it

```bash
export EVALS_GATEWAY=https://<gateway> EVALS_GATEWAY_TOKEN=<token> EVALS_SEALED_URI=s3://<bucket>/<prefix>
uv run python -m evals run t1-enwik8 piloop-0.5.0 --repeat 1 --budget 60 --hours 4
uv run python -m evals report
```

It needs Modal credentials, and read access to the sealed data. The driver itself is one light process.

## Retirement

This directory is deleted after two piloop releases in which no run used it, or when piloop gains a built-in way to
run many campaigns, whichever comes first. A task goes when no arm runs it.
