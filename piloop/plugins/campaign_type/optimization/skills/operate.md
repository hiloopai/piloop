## What status shows of the research

- the head (the latest trunk keep) and its delta over the baseline,
- one line per track when there are several: its primary at the head, the distance to its floor, its trunk keeps, its
  share against what runs,
- the trajectory of trunk keeps and the progress against the stop rule's limits,
- open runs and the ready queue,
- the live agents with their last line,
- the last 8 candidates with their delta against their parent,
- the anomalies that the daemon detects,
- the inbox and the latest decisions,
- the last events with their ages.

`piloop history [<name>]` prints the research pack: metrics, analysis artifacts, what was tried per
mechanism family, the waiting seeds, every idea, the kept lineage, the starting points, findings and digests.

Give in every digest the head against the baseline (per track when there are several), and the "Ideas by source" line
of `piloop status <name>`: it shows whether research earns its sweeps.

## Ideas

### Add an idea

```bash
PILOOP_ACTOR=orchestrator piloop idea add <name> '<title>' --hypothesis '…' --plan '…' --family '<mechanism>' \
  --from '<what prompted it: comment#…, a digest, a loss table>' --priority 1
```

- `--base <candidate>` sets the start. `--after` and `--supersedes` link it to other ideas, in the same write as the
  idea. Never add a `ready` idea and then insert its `after` link: the daemon can start it in between.
- Priority is 0 (urgent) to 4 (someday).
- Add `--proposed` when the human should look at it first.
- Your ideas carry context that research does not have: the digests, the loss analysis, the human's direction.
  Write them down as ideas as soon as you see them. Use `--from` to say where they came from.

### File a seed

A seed is a hunch that you have no time to define: a follow-up that a result suggests, two candidates worth
combining, new data.
Send it to research as soon as it comes up:

```bash
PILOOP_ACTOR=orchestrator piloop idea seed <name> '<hunch, one line>' --from '<pointers: candidate#…, comment#…, a path>'
```

- Add `--base <c>` when the hunch is about the tree of candidate `<c>`.
- The idea gets status `seed`. It shows in `piloop inbox` and `piloop watch`.
- It starts a research sweep, however full the backlog is.
- Research reads the existing work and checks for duplicates and earlier negative results. Then it does one of these:
  - It develops the seed into a defined idea with `piloop idea develop`: hypothesis, evidence, check and stop rule,
    metric, expected effect.
  - It rejects the seed with a `note` that names the evidence that settled it.
- Watchdogs file seeds the same way, and only seeds.
- Implementers file no seeds. They put a hunch in their finding.
- You may develop or reject a seed yourself when you get to it first.

A link that the human pastes (a post, a repo, a paper) is a seed signed `human`, not signed by you:

```bash
PILOOP_ACTOR=human piloop idea seed <name> '<its mechanism, one line>' --from '<link>; targets <the measured loss>'
```

### Scout for outside techniques

- This needs `[plugins.role.scout]` in `piloop.toml`, with `interval` set.
- The scout then sweeps on that timer. Each sweep searches X, GitHub and arXiv for techniques that address a measured
  loss.
- A scout sweep files at most `seeds` seeds, signed `scout#…`. The other techniques go to the `techniques` library.
- `piloop launch <name> scout` asks for a scout sweep now.
- Each scout sweep leaves a priority-3 `finding` that starts with its problem statement. Read it: a scout that
  misread the problem searched for the wrong thing.
- In your digests, report the `scout` row of "ideas by source" next to the `research` row.

### Point research at the evidence

When you or an implementer write an analysis of where the head loses (against the reference, per slice):

1. Add its path to the top-level `context` of `piloop.toml`.
2. Run `piloop init .`.

The research pack of every sweep then lists it under the analysis artifacts, newest first, before the history.

### Change an idea

Use these commands, not SQL. Each one validates, takes `--reason '<why, with ids>'`, signs the change as
`orchestrator` and records an event, so your decision's `Revert:` line is one more command.

- Park (for now), reject (never), approve, or settle without a run:
  `piloop idea status <id> parked|rejected|ready|proposed|done --reason '…'`. The reason becomes a `note` on the idea.
  An idea whose run is open (queued, running or being scored) can only be parked or rejected: its run finishes. Set
  it ready again after the run, never during it: that would queue a second run.
- Link an idea that exists: `piloop idea link <id> after|supersedes <target> --reason '…'`. It refuses a link that
  closes a cycle, and an `after` link to an idea that the daemon took already. When a prerequisite ends without a keep, the
  daemon parks the ideas after it and escalates. To run one anyway, `piloop idea unlink` it, then set it ready.
- Start it from another candidate: `piloop idea base <id> <candidate> --reason '…'`, or `head` to unpin it.
- Move it to another track: `piloop idea track <id> <track> --reason '…'`.
- Reprioritize: `UPDATE ideas SET priority = 0 WHERE id = …`, with a `note` that says why.

### Steer an idea

- A comment on the idea goes into its next brief.
- A `note`, `finding` or `decision` comment (any kind except an escalation) on the idea, or on its *running* run, is
  queued for that implementer at once. It gets only the first line (up to 200 characters) and a query for
  the full text, so put the instruction in the first line.
- `comment.queued` on the implementer says that the comment waits for it, and `comment.delivered` that it read it. It
  reads only between two tool calls. `comment.undelivered` says why it never did.
- `piloop prompt <agent-session> '<text>'` writes a note on the agent itself, which it gets in full. It says whether
  the note is queued.
- A comment on the campaign reaches no implementer. When a decision changes the work of a running implementer, write it on its
  run, not on the campaign.
- An implementer asks for what it cannot do itself with an escalation. When its run ends before your answer reaches it,
  the run ends `awaiting_answer` and its idea is parked, also when you acked the escalation. The next run reads your
  `decision` on the idea in its brief: set the idea `ready`.

### Cancel or extend a run

- A run that became redundant: `piloop run cancel <run> --wrap-up`. The implementer is told to save its work and stop,
  and ended 10 minutes later, with every process it started. Its commits are scored and its idea is parked. Without
  `--wrap-up`, it is ended at once.
- A run that needs more time than its timeout: `piloop run extend <run> --by 1h`. The implementer is told its new deadline.
- In a paused campaign, the daemon still measures what a new evaluator or `piloop candidate remeasure` needs, so the
  pause ends on a measured head. `piloop pause --evals` holds those measurements too.

### Start from a tree outside the lineage

Use this for a candidate of another campaign, or a branch.

- Run `piloop candidate import <name> <commit-or-ref> --summary '…'`. It registers a `reference` candidate.
  Never `INSERT INTO candidates`.
- The daemon measures it.
- Start an idea from it with `piloop idea add <name> '<title>' --base <c>`.
- A candidate of another campaign is its `revision`. If it is in another repo, fetch it into this repo first.
- The daemon never judges a reference. It is the bar only of a run that is pinned to it.
- A composition that you made by hand on the head: import it, wait for its measurement or add yours
  (`piloop measurement add`), then `piloop candidate verdict <c> keep --reason '…'`. It is the head then.

Branch runs:

- A run pinned to a reference, or to an older keep, is a branch run. It is judged against its own base.
- Its keeps form a branch and never move the head. The overview lists the latest keep of each lineage.
- To bring a branch keep onto the trunk, file an unpinned idea that merges it into the head.
- A noted candidate (`piloop candidate note`) or any other non-keep is not a bar. A run pinned to one starts from its
  tree and is judged against the nearest keep. When that keep is the head, it is a trunk run.
- A run pinned to a candidate that has the head's changes (a merge onto the head, or a compose that cherry-picked it)
  is a trunk run too: its keep can move the head.
- So pin a follow-up to a lead. Do not tell its implementer to reset onto the lead.
- One run in `[plugins.role.implementer] explore_every` (default 4) takes the most urgent ready idea pinned to a base other than
  the head, whatever its priority.

### Keep a failed result as a lead

```bash
piloop candidate note <c> '<why>' --follow-up '<hunch>'
```

- `--follow-up` is repeatable.
- It changes a measured `discard` or `interesting` candidate to `interesting` with the cause `note` and your reason.
- It files each follow-up as a seed pinned to the candidate. Research develops the seeds.
- It is not a keep and not a bar.
- Implementers note a lead with a `Piloop-Note:` commit trailer, without follow-ups.

### Refill the backlog

- Run `piloop launch <name> research`.
- A sweep develops the seeds first. Then, while the backlog is short, it adds up to `max_ideas` ideas of its own.
- It leaves a `finding` on the campaign.
- Research sweeps also run by themselves while the backlog is below its target. They wait longer after sweeps that
  define nothing.
