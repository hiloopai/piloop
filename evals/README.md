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
  that scores every arm afterwards. The run ends at
  the budget, at `max_hours`, or at the campaign's own stop; then the daemon gets `EVALS_STOP_GRACE_MINUTES` to
  score what was committed.
- **plain arm:** one Codex or Claude Code session, told to keep `main` at its best version. When it ends early, it
  is resumed with the same short prompt.

The driver reads the spend every `EVALS_POLL_SECONDS`: the tokens of every session file in the sandbox at list
price, plus the sandbox's CPU and memory. The export (git bundle, piloop's database, session files, spend) goes to
`results/<task>/<arm>-<repeat>/`, and to `EVALS_ARCHIVE_URI` when it is set.

Scoring runs the base and every head of a run in sandboxes without network: the task's own `evaluate.py` over the
head's tree gives the dev score, and `sealed.py` the sealed one, with the data from `EVALS_SEALED_URI/<task>/`.
Heads are a piloop arm's keeps and a plain arm's commits on `main`.

## Metrics

`result.json` holds, per run: the sealed and dev score of each head against $ and hours, $ and hours to the first
keep, the funnel (ideas, runs, submissions, kept), escalations, comments by people, the lead's share of the tokens,
and the gap between the dev and sealed score of the final head. The report draws each arm's median curve over
repeats with the band from its lowest to its highest repeat, with plotly, as static pages.

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
