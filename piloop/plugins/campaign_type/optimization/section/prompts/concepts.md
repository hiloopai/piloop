- A **campaign** is {vcs:repo} with a `piloop.toml`.
- **Ideas** are things worth trying. When an idea is `ready`, the scheduler creates a **run**: one attempt, on one
  harness and model. It starts an **implementer** process in {vcs:checkout} at the run's base **candidate**.
  `runs.end` says how a finished run ended, and `runs.error` gives the detail. Only `finished`, `failed`, `timed_out`
  and `awaiting_answer` count as attempts: `cancelled`, `campaign_stopped`, `daemon_stopped`, `stale`, `worker_lost`,
  `role_removed` and `not_started` say that something else ended the run.
- {vcs:versions}
- The daemon scores each new candidate with the campaign's **evaluator** into a **measurement** (metrics, or an
  error). The evaluator is the evaluate command, the linked inputs and the judging metrics (primaries and
  guardrails) as of the last config sync. A change makes a new evaluator row; a watch metric is not part of it. A candidate can have many measurements: repeats, or a newer evaluator.
- A candidate's **verdict** (`keep`, `interesting`, `discard` or `crash`) is the daemon's judgement of its mean score
  against its bar. The bar is the run's last keep, starting at its base. The orchestrator or a human may override it.
  `verdict IS NULL` until the candidate is judged.
- A candidate's **origin** says where it comes from: `baseline`, `run`, `reference`, `recovered` or `skipped`. Only
  the first two are judged. A pending candidate is `verdict IS NULL AND origin IN ('baseline', 'run')`.
  {vcs:skip}
- Every run that pins no base starts from the **head**: the latest *trunk keep*, which is a keep that no branch run
  made. So the trunk is a chain of keeps.
- A candidate that keeps against its bar after the head moved on (another run kept meanwhile) is not kept. It is
  `interesting`. The daemon merges it onto the head and judges the merge there (*rebase-on-keep*). A merge that
  conflicts files a seed to compose the two by hand.
- A run whose idea pins a base other than the head (`runs.branch`: from a reference, an older keep or a note) is judged
  against its own base only. Its keep is a *branch keep*, which never moves the head.
- A non-keep is not a bar. A run pinned to one (a note, a discard) starts from its tree and is judged against its
  nearest kept ancestor. It is a trunk run when that ancestor is the head.
- A `reference` (`origin = 'reference'`) is a starting point imported with `piloop candidate import`, with no run and no
  parent. The daemon measures it but never judges it. An idea can pin it as `base_candidate_id`. Kept by hand, it is
  the head.
- A campaign can have several **tracks** (objectives), each with its own primary metric. `ideas.track` names an idea's
  track. A candidate is judged on every track whose primary it has, its idea's track first, and gets the best verdict:
  `candidates.track` names its track. On a track, the primary of every other track is one more guardrail, with its
  margin and floor. A campaign with one primary has one track, named after that metric.
- **Comments** attach to any row. Agents and humans talk to each other through them.
- **Events** are the append-only log of what happened.
