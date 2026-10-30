## What status shows of the research

- the head (the latest trunk keep) and its delta over the baseline,
- one line per track when there are several: its primary at the head, the distance to its floor, its trunk keeps, its
  share against what runs,
- the trajectory of trunk keeps and the progress against the stop rule's limits,
- open runs and the open ideas,
- the live agents with their last line,
- the last 8 candidates with their delta against their parent,
- the anomalies that the daemon detects,
- the inbox and the latest decisions,
- the last events with their ages.

`piloop history [<name>]` prints the research pack: metrics, analysis artifacts, every idea, the kept lineage, the starting points, the agents' notes and digests.

Give in every digest the head against the baseline (per track when there are several), and the "Ideas by source" line
of `piloop status <name>`: it shows whether research earns its sweeps.

## Ideas

### Add an idea

```bash
PILOOP_ACTOR=orchestrator piloop idea add <name> '<title>' --hypothesis '…' --plan '…' \
  --from '<what prompted it: comment#…, a digest, a loss table>' --priority 1
```

- `--after` links it to other ideas, in the same write as the idea. Never add an `open` idea and then insert its
  `after` link: a worker can claim it in between.
- Priority is 0 (urgent) to 4 (someday).
- Your ideas carry context that research does not have: the digests, the loss analysis, the human's direction.
  Write them down as ideas as soon as you see them. Use `--from` to say where they came from.

### Add the human's link

A link that the human pastes (a post, a repo, a paper) is an idea signed `human`, not signed by you:

```bash
PILOOP_ACTOR=human piloop idea add <name> '<its mechanism, one line>' --from '<link>; targets <the measured loss>'
```

### Point research at the evidence

When you or an implementer write an analysis of where the head loses (against the reference, per slice):

1. Add its path to the top-level `context` of `piloop.toml`.
2. Run `piloop init .`.

The research pack of every sweep then lists it under the analysis artifacts, newest first, before the history.

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
- `comment.queued` on the implementer says that the message waits for it, and `comment.delivered` that it read it. It
  reads only between two tool calls. `comment.undelivered` says why it never did.
- `piloop prompt <agent-session> '<text>'` writes a message to the agent itself, which it gets in full. It says whether
  the message is queued.
- A message on the campaign is for you, not for an implementer. To change the work of a running implementer, write a
  message on its run.
- An implementer asks for what it cannot do itself with an escalation. The escalation holds nothing: the run goes on,
  and ends when its implementer runs `piloop run end` or at its deadline. Answer with a message on the run while it
  runs, or with a note on the idea for the next run.

### Cancel or extend a run

- A run that became redundant: `piloop run cancel <run> --wrap-up`. The implementer is told to save its work and stop,
  and ended 10 minutes later, with every process it started. Its commits are scored and its idea is dropped. Without
  `--wrap-up`, it is ended at once.
- A run that needs more time than its timeout: `piloop run extend <run> --by 1h`. The implementer is told its new deadline.
- In a paused campaign, the daemon still measures what a new evaluator or `piloop candidate remeasure` needs, so the
  pause ends on a measured head. `piloop pause --evals` holds those measurements too.

### Start from a tree outside the lineage

Use this for a candidate of another campaign, or a branch.

- Run `piloop candidate import <name> <commit-or-ref> --summary '…'`. It registers a `reference` candidate.
  Never `INSERT INTO candidates`.
- The daemon measures it.
- To take its change, add an idea whose hypothesis names the candidate and its `revision`. Every run starts from the
  head, and its worker merges the change in.
- A candidate of another campaign is its `revision`. If it is in another repo, fetch it into this repo first.
- The daemon never judges a reference.
- A composition that you made by hand on the head: import it, wait for its measurement or add yours
  (`piloop measurement add`), then `piloop candidate verdict <c> keep --reason '…'`. It is the head then.

Every run starts from the head, so its keeps move the head. A branch keep of an older campaign stays on its branch:
to bring it onto the trunk, add an idea that merges it into the head.

### Keep a failed result as a lead

```bash
piloop candidate note <c> '<why>' --follow-up '<hunch>'
```

- `--follow-up` is repeatable.
- It changes a measured `discard` or `interesting` candidate to `interesting` with the cause `note` and your reason.
- It adds each follow-up as an open idea about the candidate. Its worker takes the candidate's change onto the head.
- It is not a keep and not a bar.
- Implementers note a lead with a `Piloop-Note:` commit trailer, without follow-ups.

### Refill the backlog

- Run `piloop launch <name> research`.
- While the backlog is short, a sweep adds up to `max_ideas` ideas.
- It leaves a `note` on the campaign.
- Research sweeps also run by themselves while the backlog is below its target. They wait longer after sweeps that
  define nothing.
