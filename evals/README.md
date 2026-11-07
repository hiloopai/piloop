# evals

Runs piloop configurations and plain agent sessions on the same tasks, at the same $ and time budget, and scores
what each hands in on data that no agent saw.

- `tasks/<name>/`: `repo/` is the starting commit, with its brief and its dev evaluator. `task.toml` names the dev
  metric, the dev scores that count as 0 and 1, the dev data and the task's own `piloop.toml` keys. `sealed.py` is
  the sealed scorer; the data it reads lives outside this repo. It writes `score`, `status`, `detail` and `raw`, and
  may add `items`, `{name: bool}`: whether the version passed each item of the holdout, such as a hidden test.
- `arms/<name>.toml`: a piloop install with its agents and extra `piloop.toml` keys, or one plain agent session.
- `python -m evals run <task> <arm>` runs one repeat, then scores it. `rescore <run dir>` scores a run again, and
  `report` writes one HTML page per task.

## How a run works

Every sandbox of a run starts from the same image: the task's repo at the same starting commit, its dev data, the agent
CLIs (Codex, Claude Code and pi), and, for a piloop arm, that arm's piloop with a fresh home. The agents call models
through the gateway that `EVALS_GATEWAY` names, with `EVALS_GATEWAY_TOKEN`. A pi agent runs an `anthropic/<id>` model,
and its image points pi's Anthropic provider at the gateway (`models.json`): pi takes no variable for it.

- **piloop arm:** `piloop init` in the repo, with the task's keys, the arm's agents and `max_hours`. An arm with
  `split = "sealed"` also gets the task's dev command as `[evaluator] dev`, and a sealed split of its own as
  `[evaluator] sealed`: `EVALS_SEALED_URI/<task>.campaign/`, in the evaluators' image and no agent's. It is not the
  holdout that scores every arm afterwards. The run ends at the budget, at `max_hours`, or at the campaign's own stop;
  then the daemon gets `EVALS_STOP_GRACE_MINUTES` to score what was committed.
- **plain arm:** one Codex, Claude Code or pi session, told to keep `main` at its best version. When it ends early, it
  is resumed with the same short prompt. It runs in a Modal sandbox of the arm's `resources`, or with
  `EVALS_PLATFORM=daytona` in one of 2 CPUs and a hard 8 GiB, as each agent of a piloop arm.

A piloop arm runs as a team (`drive.on_team`):

- Its daemon (`piloop serve --listen`, `piloop cas serve`) runs in a control sandbox on Daytona, which takes no job.
  Its `config.toml` gives it two pools whose provisioners start the rest at once, and drain it when the run is over.
- The pool `agents`: a Modal sandbox for each worker (the runs it keeps open at once), with 2 CPUs and a hard 8 GiB
  (or the task's `memory_limit_mb`), reaching what the task's `network` allows and the control sandbox.
- The pool `eval`: `EVALS_EVALUATORS_PER_WORKER` evaluators for each worker, one evaluation at a time each, where the
  task is scored: on Daytona for a task with `scorer = "daytona"`, else on Modal at the scorer's size. So every
  timing that decides a verdict runs where the holdout is scored, with hard limits.
- A Daytona sandbox starts from a snapshot of its image, one per image and size. When the org is at its snapshot quota, the driver deletes the least recently used `evals-` snapshot that no sandbox uses, and tries once more: a run that needs it again builds it again.
- The control sandbox keeps the gateway's token and the platforms' keys in a file that its daemon reads, never in its
  environment. It needs `EVALS_DAYTONA_API_KEY` and Modal's token (`modal token new`).

A run holds its agent sessions in `EVALS_SESSION_LEDGER` while it lives. The driver refuses a run that would take the
sessions of every run on this machine over `EVALS_MAX_SESSIONS`. Sessions that no driver here starts, such as another
campaign's on the same gateway pool, are a file there with `run`, `sessions` and `until`.

The driver reads the spend every `EVALS_POLL_SECONDS`: the tokens of every agent's session file at list price, plus
the sandboxes' CPU and memory. Each sample keeps the tokens too, by kind. The driver reaches only the control sandbox.
A team's workers put each agent's session file into the control's CAS as it grows, at most 30 s behind; the driver
copies the latest snapshot of each session to `agents/<harness>/<session id>.jsonl` there. The export (git bundle,
piloop's database, session files, spend) goes to `results/<task>/<arm>-<repeat>/`, and to `EVALS_ARCHIVE_URI` when it
is set. Session files that could not be brought up to date are named in `run.json`'s `export_failed`, and the rest
of the export still goes. A sandbox is deleted only after its export.

To stop a run early, write the reason into a file `halt` in its directory: the driver stops at its next sample and
exports. Never signal the driver: Modal's client ends the sandboxes that it made when its process is interrupted.

Scoring runs the base and every head of a run in sandboxes without network: the task's own `evaluate.py` over the
head's tree gives the dev score, and `sealed.py` the sealed one, with the data from `EVALS_SEALED_URI/<task>/`.
Heads are a piloop arm's keeps (before piloop 0.6) or its best over time (0.6's pool: each candidate that a score made
the best), and a plain arm's commits on `main`. The driver tells the two piloop schemas apart by the database. A task that times its runs scores each version in
`score_repeats` sandboxes and takes the means; `scores.json` keeps each scoring with its CPU. A task's `cloud` pins
the CPU type of its sandboxes, the agents' and the scorers'.

When the sealed scorer reports `items` for the base, scoring goes on to every submission and its parent: a piloop
arm's candidates from its implementers (both schemas), with the candidate each came from, and a plain arm's commits on
`main`'s first-parent line, with their first parent, as the session commits whatever it likes there. Each tree is
scored once, so a head or a revert costs nothing more. This costs a sandbox per submission and repeat, at
`score_concurrency` at a time: the 1-worker runs so far handed in 16 to 60 submissions, a 10-worker run over 600,
which takes hours. Each score goes into `scores.json` as soon as its version is scored. A scoring that fails, such as on a 502 of the platform's API, is tried `EVALS_SCORE_ATTEMPTS` times; a version whose scoring still fails is left out, and the others are kept. `rescore` scores only what `scores.json` lacks: to count the regressions of a run scored before its task's
scorer had items, remove its `scores.json` first.

## Metrics

`result.json` holds, per run: the sealed and dev score of each head against hours, $, the models' $ and the tokens, $ and hours to the first
keep, the funnel (ideas, runs, submissions, kept), escalations, comments by people,
the gap between the dev and sealed score of the final head, and the regressions: the submissions, those scored, and
each scored submission that fails an item that its parent passed, with the items it lost (`regressions.found`; null for
a task without items). A crash fails every item. The report gives their share of the scored submissions. Spend is split into models and compute. Before the agents
start, the driver evaluates the starting repo 5 times with the dev command and records the CPU (`selftest`): how much
the evaluator varies on that machine. Dev evaluations are counted where they run: `wrap/sitecustomize.py`, on the
sandbox's `PYTHONPATH`, logs each run of `evaluate.py`, however it was started. A piloop arm also records each role's
keeps (0.6: new bests), and those of them that the final head contains (0.6: the candidates on the final best's line).
Before 0.6 it records its races: submissions that beat the version they started from but not the head that another
run had moved on to. A 0.6 run records its confirmations, its sealed scorings and the runs that chose another base. The report draws each arm's median curve over repeats with
the band from its lowest to its highest repeat, with plotly, as static pages.

## Telemetry

Each step of the driver is a span: the run, its sandbox, each spend sample, the stop, the export, and the scoring of
each version. A span prints one line on the console when it ends. With `OTEL_EXPORTER_OTLP_ENDPOINT` (and
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
