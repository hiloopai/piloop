---
name: piloop-operate
description: Operate a running piloop autoresearch campaign as its orchestrator, from any harness with a shell (Claude Code, Codex, pi, Gemini CLI, Cursor), by looping on piloop inbox wait to decide escalations, manage ideas, steer implementers and read the pool. Use when the human asks you to orchestrate, watch, run or report on a campaign, when an escalation arrives, or when you take a decision for the human; do not use it in an agent that piloop launched (PILOOP_ACTOR is an agent id, such as implementer#12).
---

# Operate a piloop campaign

The database is the contract. Everything below is a `piloop` command in your shell, SQL included.

- Run SQL with `piloop sql -`, the statement piped in from a quoted heredoc (`<<'EOF'`, with the quotes).
- Never put your own text inside a double-quoted shell argument: the shell runs what is in backticks and `$(…)`, and
  expands `$VAR`, before piloop sees it. Give other commands short values in single quotes, without a `'` in them,
  and write anything longer as a comment through `piloop sql -`.
- Read `piloop schema` once before your first write. It gives the conventions and every table.
- In a numbered seat, use the inherited `PILOOP_ACTOR` for every command and SQL signature. Substitute
  `"$PILOOP_INBOX_ID"` for the literal `orchestrator` actor in the examples below.
- In a single unnumbered session, run every piloop command as `PILOOP_ACTOR=orchestrator piloop …`. When the human runs a second install under
  another command, such as `piloop-canary` next to a release, use that command wherever this skill says `piloop`.
  - The rows you change, the items you acknowledge and their events are then signed `orchestrator`.
  - `piloop inbox wait` then skips what you wrote yourself.
- Limit your queries to the campaigns that you orchestrate: the campaign in the current directory
  (`SELECT id, name FROM campaigns WHERE path = '<cwd>'` gives `<p>` and `<name>`), or each campaign that the human names.
  For several campaigns, leave the name out of `piloop inbox wait`: it then waits for every campaign.

## The loop

Loop on one blocking command. It costs no turns while nothing happens:

```bash
PILOOP_ACTOR=orchestrator piloop inbox wait [<name>] --timeout 1800   # first call: from now
PILOOP_ACTOR=orchestrator piloop inbox wait [<name>] --after <cursor> --timeout 1800   # every later call
```

It returns, one line each, as soon as something needs you:

- a new unacknowledged inbox item that someone else wrote,
- a campaign that the daemon stopped,
- the daemon's score of a candidate.

Otherwise it returns `nothing new` at the timeout.
Its last line, after `next: `, is the command to wait again, with the cursor (`--after <event-id>`), so you miss
nothing that arrives between two waits. Run it in the form above, with `PILOOP_ACTOR=orchestrator` and your
`--timeout`.

Do not let your shell tool kill the wait:

- Claude Code: run it in the background (`run_in_background`). You are woken when it exits.
- Codex and pi: set the timeout parameter of the shell tool above `--timeout`.
- When the tool limit is lower than 30 minutes, pass a `--timeout` below the limit. Use a shorter wait, never a polling
  loop of your own.

Steps:

1. **Start.** Run `piloop status <name>` and `piloop inbox` first, for each campaign. Items that already wait are yours
   too: the wait reports only new items. Then read what earlier sessions noted on the campaign (step 7), with
   `piloop sql -`: `SELECT id, created_at, body FROM comments WHERE target_kind = 'campaign' AND target_id = <p> AND author =
   'orchestrator' AND kind = 'note' ORDER BY id DESC LIMIT 20`.
   Then run `piloop doctor`, and again once a day. Run the commands that it names exactly as it names them: a second
   install, such as `piloop-canary`, names its own command, so that you never restart or upgrade the other install.
   - `FAIL daemon: … runs older code than the installed …`: run its fix, the restart. The running agents keep going.
   - `cli: … is out`: a newer piloop is available. When the line says that `PILOOP_AUTO_UPGRADE` is on, run the
     upgrade that it names with `PILOOP_ACTOR=orchestrator`, and record it as a decision. Otherwise do not upgrade:
     file a p2 escalation on the campaign that asks the human, with the line and its changes link. An upgrade
     migrates the database one way, and the runs after it are scored by the new code.
   - Any other `FAIL`: fix it as its `fix:` line says, or escalate it.
2. **Wait.** Run the command above.
3. **Read** each line.
   - An item: `piloop inbox show <id>` shows it with its thread.
   - A stop: `piloop status <name>`.
4. **Decide and record.** See "The inbox" below: escalations, every decision, the surfacing policy.
5. **Acknowledge** what you read and need not answer: `PILOOP_ACTOR=orchestrator piloop inbox ack <id>`. It leaves
   your waits only; the human still sees it. Your answer leaves every inbox. Leave unanswered what only the human can
   decide.
6. **Act.** Apply the decision with a piloop command: `piloop pause`, a command of the campaign's type (below),
   or `piloop.toml` followed by `piloop init .`. Use `piloop sql` for a write only when no command does it.
   Never raise `hold_at` in `[plugins.stop_rule]` yourself. An escalation that a fact reached its `hold_at` belongs to
   the human.
7. **Note** each plan and result as you go, as a `note` comment on the campaign with its ids, and no priority, so that
   it stays out of the inbox: `INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('campaign',
   <p>, 'orchestrator', 'note', 'dropped idea#412 (dup of #398); next: c#486+c#501')`, piped into `piloop sql -`. A
   session that loses its context (a crash, a full window) starts again from step 1, so write what the next session
   needs, not what the database already says. A decision is a `decision` comment (below).
8. **Report** when the human is present, at least every few waits or every hour. Then go back to step 2.
   - Give one short status: what ran, what you decided. `piloop status <name>` has the numbers.
   - Include the progress line of `piloop status <name>` in every digest, with the spend.
   - When every campaign you watch has stopped, report the result and stop the loop.

Run the loop yourself, or give it to a background subagent so that the human's session stays free. Give the subagent
this skill and the campaign name. Tell it to report decisions and status back.

In multi-orchestrator mode, each seat keeps its own wait cursor and acknowledgements. Read the current board and
consult peers before overlapping changes. Coordinate through campaign messages, or use `piloop message <name>
'<text>' --seat N` to address seat N. Workers and ideas are shared. Answer escalations before acting on them.
The first accepted decision wakes the other seats. If a peer answered first, read that decision before acting.

## Status

`piloop status [<name>]` shows the overview of a campaign: its best, its agents, its last candidates and its inbox.
Without a name, it uses the campaign in the current directory, else the only active campaign, else every campaign.

`piloop watch` shows the same as `piloop status`, live, in the human's terminal.

If the daemon is down, `requested` rows collect in `agent_sessions` and nothing runs.
Check with `SELECT id, kind, reason FROM agent_sessions WHERE state = 'requested'`.
Start the daemon with `piloop serve --detach`.
