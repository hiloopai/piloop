## What status shows of the research

- the best of each track and its delta over the baseline,
- one line per track when there are several: its primary at its best against the baseline, the distance to its
  floor, its new bests, its open runs and open ideas,
- the goal, and the progress against the stop rule's limits,
- the trajectory: the baseline, the best, the latest new bests, and the other lines worth branching from,
- open runs and the open ideas,
- the live agents with their last line,
- the last 8 candidates with where each stands and its delta against its parent,
- the anomalies that the daemon detects,
- the inbox and the latest decisions,
- the last events with their ages.

`piloop history [<name>]` prints the research pack: metrics, analysis artifacts, every idea, the new bests, the starting points, the agents' notes and digests.

Give in every digest the best against the baseline (per track when there are several), and the "Ideas by source" line
of `piloop status <name>`: it shows which source of ideas earns its runs.

## Ideas

### Add an idea

```bash
PILOOP_ACTOR=orchestrator piloop idea add <name> '<title>' --hypothesis '…' --plan '…' \
  --from '<what prompted it: comment#…, a digest, a loss table>' --priority 1
```

- `--after` links it to other ideas, in the same write as the idea. Never add an `open` idea and then insert its
  `after` link: a worker can claim it in between.
- Priority is 0 (urgent) to 4 (someday).
- Your ideas carry context that the workers do not have: the digests, the loss analysis, the human's direction.
  Write them down as ideas as soon as you see them. Use `--from` to say where they came from.
- An idea is a direction in at most 150 words: what to try and why. Never how many variants to run, a limit on a run,
  or what not to try: the worker decides how.

### Add the human's link

A link that the human pastes (a post, a repo, a paper) is an idea signed `human`, not signed by you:

```bash
PILOOP_ACTOR=human piloop idea add <name> '<its mechanism, one line>' --from '<link>; targets <the measured loss>'
```

### Point the agents at the evidence

When you or an implementer write an analysis of where the best loses (against the reference, per slice):

1. Add its path to the top-level `context` of `piloop.toml`.
2. Run `piloop init .`.

`piloop history` then lists it under the analysis artifacts, newest first, before the history.

### Change an idea

Use these commands, not SQL. Each one validates, takes `--reason '<why, with ids>'`, signs the change as
`orchestrator` and records an event, so your decision's `Revert:` line is one more command.

- Drop it, or open it again: `piloop idea status <id> dropped|open --reason '…'`. The
  reason becomes a `note` on the idea. A claimed idea, whose run is open (queued, running or being scored), can only
  be dropped: its run finishes. Open it again after the run, never during it: a second run could claim it.
- Link an idea that exists: `piloop idea link <id> after <target> --reason '…'`. It refuses a link that closes a
  cycle, and a link from a claimed idea. An idea is held until each prerequisite is `done` or `dropped`. To free it
  sooner, `piloop idea unlink` it.
- Move it to another track: `piloop idea track <id> <track> --reason '…'`.
- Reprioritize: `UPDATE ideas SET priority = 0 WHERE id = …`, with a `note` that says why.

### Steer an idea

- A `note` on the idea is on the board: the runs that claim it later read it. It reaches no running implementer.
- A `message` on the idea, or on its *running* run, is queued for that implementer at once. It gets only the first
  line (up to 200 characters) and a query for the full text, so put the instruction in the first line.
- `comment.queued` on the implementer says that the message waits for it, and `comment.delivered` that it read it: at
  its next `piloop` command, or for Claude after its next tool call. `comment.undelivered` says why it never did, such
  as an implementer that ended first.
- `piloop prompt <agent-session> '<text>'` writes a message to the agent itself, which it gets in full. It says whether
  the message is queued.
- A message on the campaign is for you, not for an implementer. To change the work of a running implementer, write a
  message on its run.
- An implementer asks for what it cannot do itself with an escalation. The escalation holds nothing: the run goes on,
  and ends when its implementer exits, runs `piloop run end`, or reaches its deadline. Answer with a message on the run while it
  runs, or with a note on the idea for the next run.

### Cancel or extend a run

- A run that became redundant: `piloop run cancel <run> --wrap-up`. The implementer is told to save its work and stop,
  and ended 10 minutes later, with every process it started. Its commits are scored and its idea is dropped. Without
  `--wrap-up`, it is ended at once.
- A run that needs more time than its timeout: `piloop run extend <run> --by 1h`. The implementer is told its new deadline.
- In a paused campaign, the daemon still measures what a new evaluator or `piloop candidate remeasure` needs, so the
  pause ends on a measured best. `piloop pause --evals` holds those measurements too.

### Start from a tree outside the pool

Use this for a candidate of another campaign, a branch, or a composition that you made by hand.

- Run `piloop candidate import <name> <commit-or-ref> --summary '…'`. It registers a `reference` candidate.
  Never `INSERT INTO candidates`.
- The daemon scores it into the pool, as any candidate: it may become the best.
- A candidate of another campaign is its `revision`. If it is in another repo, fetch it into this repo first.
- To build on it, note it with a follow-up (below): the follow-up's run can start from it with `--base`.

### Point the runs at a candidate

```bash
piloop candidate note <c> '<why it is worth starting from>' --follow-up '<hunch>'
```

- `--follow-up` is repeatable.
- The note is a comment on the candidate. It changes no score.
- Each follow-up is an open idea about the candidate, whose run can start from it
  (`piloop idea claim <id> --base <c>`).

### An empty board

- Nothing waits for one: each worker proposes its own idea when the board has none for it.
