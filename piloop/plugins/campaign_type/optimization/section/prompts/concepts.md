- A **campaign** is {vcs:repo} with a `piloop.toml`.
- An `open` **idea** gets a **run**: one attempt, on one harness and model, by an **implementer** in {vcs:checkout} at
  the run's base **candidate**. `runs.end` says how it ended, `runs.error` the detail. Only `finished`, `gave_up`,
  `failed` and `timed_out` count as attempts. A worker ends its own run, as submitted or given up.
- {vcs:versions}
- The daemon scores each new candidate with the campaign's **evaluator** (the evaluate command, its linked inputs and
  the judging metrics) into a **measurement**. A changed config makes a new evaluator row.
- A candidate's **verdict** (`keep`, `interesting`, `discard`, `crash`) judges its mean score against its bar: the
  run's last keep, starting at its base. `verdict IS NULL` until it is judged. Its **origin** is `baseline`, `run`,
  `reference`, `recovered` or `skipped`, and only the first two are judged. {vcs:skip}
- The **head** is the latest *trunk keep*. A run that pins no base starts from it. A keep against a bar that the head
  has passed is `interesting`, and the daemon merges it onto the head and judges the merge there (*rebase-on-keep*).
- A `reference` (`piloop candidate import`) is measured, never judged.
- A campaign can have several **tracks**, each with its own primary metric (`ideas.track`, `candidates.track`). On a
  track, every other track's primary is one more guardrail.
- **Comments** attach to any row. **Events** are the append-only log of what happened.
