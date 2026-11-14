- A **campaign** is {vcs:repo} with a `piloop.toml`.
- A **run** is one session, on one harness and model, of an **implementer** in {vcs:checkout} at the run's base
  **candidate**. It claims `open` **ideas** (`run_claims`), and each submission is for one of its claims. `runs.end`
  says how it ended, `runs.error` the detail. Only `finished`, `gave_up`, `failed` and `timed_out` count as attempts.
  A worker ends its own run, as submitted or given up. An idea is done once a submission for it has a score.
- {vcs:versions}
- The daemon scores each new candidate with the campaign's **evaluator** (the evaluate command, its linked inputs and
  the metrics that decide) into a **measurement**. A changed config makes a new evaluator row.
- Every scored candidate is in the campaign's **pool**: nothing is kept or discarded. A candidate whose first score
  beats the best's is evaluated until it has `[evaluator] noise_runs` evaluations: it is **confirmed**. The **best**
  of a track is the confirmed candidate with the best mean that the best rule allows, else the baseline. Its
  **origin** is `baseline`, `run`, `reference`, `recovered` or `skipped`; the last two are never scored. {vcs:skip}
- A candidate's **line** is its chain of parents. A run starts from the best, or from the candidate that its claim
  names (`--base <c>`).
- A `reference` (`piloop candidate import`) is scored like any other candidate.
- A campaign can have several **tracks**, each with its own primary metric (`ideas.track`) and its own best. On a
  track, every other track's primary is one more guardrail.
- **Comments** attach to any row. **Events** are the append-only log of what happened.
