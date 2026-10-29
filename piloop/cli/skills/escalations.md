## The inbox: decide, record, keep moving

The human is often away and does not want to block work. Nothing waits on them.

- You decide what comes up and keep the campaign moving.
- Every decision you take for the human becomes an item in their inbox, so that they can review and change it later.

The inbox is every comment that has a `priority`.

- `piloop inbox` lists the unacknowledged items, most urgent first.
- `piloop inbox show <id>` shows one item with its thread.
- `piloop inbox ack <id>` (or `--up-to <id>`) marks items as seen.

The priority says how urgent an item is:

- **0–1:** now.
- **2–4:** can wait. Status counts them.
- **No priority:** the log.

### Escalations

An escalation arrives as a `comment#<id> escalation …` line from `piloop inbox wait`. It is already an inbox item: its
writer gave it a priority.

1. **Read** the thread and the target.
   - `piloop inbox show <id>`.
   - For a run: `SELECT status, end, error, log_dir FROM runs WHERE id = <t>`, and the end of `<log_dir>/transcript.txt`.
   - The agent's own session, the harness's file: `SELECT session_file FROM agent_sessions WHERE subject_kind = 'run' AND subject_id = <t>`, then
     `piloop cas get <session_file>`.
   - Verify before you build on a theory: the agent's own transcript, `ps`, a new query. Do not guess the mechanism.
2. **Decide.**
   - If the human is talking to you now, give them the one decision, with the evidence and your recommendation.
   - Otherwise decide yourself. Take the smallest reversible step that keeps work moving.
   - An irreversible step that can wait is not yours: leave it unacknowledged. Too low a priority? Raise it with SQL.
   - Answer one that offers choices with `piloop inbox choose <id> <choice>`. Give duplicates the same answer. Ask the
     human with `piloop escalate <target>` (its form on stdin; `--guide`), citing in `details` what it settles.
3. **Record** the decision on the same target, addressed to the human (use the template below).
   - Then acknowledge the escalation: `PILOOP_ACTOR=orchestrator piloop inbox ack <id>`. Your decision replaces it in
     the inbox.
   - If the human decided it with you, the decision is theirs. Record it without a priority, and acknowledge it as the
     human: `piloop inbox ack <id>`, with no `PILOOP_ACTOR`.
4. **Apply** it: un-park or reject the idea, change its priority, or fix `piloop.toml` and run `piloop init .`.
