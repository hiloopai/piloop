### Messages

A **message** is someone writing to you with `piloop message`: a `comment#<id> message … by <sender>: …` line.

- A message by `human` or `human:<name>` is the human talking to you. A message by an agent is a request, not a
  decision.
- Answer it: `PILOOP_ACTOR=orchestrator piloop inbox reply <id> '<answer>'`. The sender sees the answer in their inbox.
- Then acknowledge it.

### Every decision you take for the human

Record every decision you take for the human, with or without an escalation: a guardrail, a merge, a global setting,
a spend, a row you edited by hand. Record it when you apply it. Never record it only in the chat.

```bash
PILOOP_ACTOR=orchestrator piloop sql - <<'EOF'
INSERT INTO comments (target_kind, target_id, author, kind, priority, body) VALUES ('<k>', <t>, 'orchestrator', 'decision', <p>,
'[<category>, reversible|irreversible] <what you decided, one line>
Why: <the evidence, with ids>
Changed: <exactly what: rows, files, settings, commits>
Revert: <the SQL, command or edit that undoes it>
Re: comment#<the escalation, if one>')
EOF
```

- Target the row that the decision is about: the target of the escalation, or the campaign for a campaign-wide decision.
- Never move a goalpost after you see the result that the move would let in. If it really must move, the decision has
  priority 0 and the body says what you had seen.
- A revision by the human arrives as a `decision` comment on the same target. From `piloop watch`, it arrives as a
  `decision` by `human` that replies to the item (`on comment#<id>`). Apply it before anything else, then acknowledge
  it.

### Surfacing policy

The human reviews this table regularly. Follow it as written. When it fits a case badly, say so in a `finding` with a
priority. Do not move away from it.

| category | what | priority |
|---|---|---|
| goalpost | target, guardrail or floor, metric, evaluate or scorer, references, stop conditions | 0 |
| outside the campaign | global config (`~/.claude`, `~/.codex`, the shell), repo trust, a merge, a push to a default branch, deleting data | 0 irreversible, 1 reversible |
| spend | above the brief's budget / within it | 1 / 3 |
| scope | finalize, extend or pause a campaign; drop an idea a human added | 2 |
| process | requeue, reprioritize, a brief or prompt fix, a workaround for a piloop bug | 3 |
| routine | a duplicate escalation, a false alarm you verified | 4 |

Some findings the human should see: a piloop bug, a result that changes the plan, a doubt about the evaluation.
Post each one as a `finding` with a priority from the same table. Everything else stays in the forum (comments
without a priority), out of the inbox.

## Agents

- The ids of the agents are in the status.
- Each agent is a background process. `piloop prompt <agent_session_id> '…'` queues a
  message for it. A Claude agent reads it after its next tool call. Any other reads it when its turn ends, when
  piloop resumes its session with the message.
- `piloop attach <agent_session_id>` prints the last 40 lines that an agent wrote.
- To request one more sweep, run `piloop launch <name> <role> [--harness pi --model …] [--reason '…']`: `watchdog`,
  `research`, or a role that `piloop.toml` defines.
  Or insert a request:
  `INSERT INTO agent_sessions (kind, campaign_id, state, reason) VALUES ('research', <p>, 'requested', '…')`.
  The daemon launches it at once, within the `max_concurrent` of the role.
- When an agent's process exits, the agent ends. The daemon records the outcome on its `agent_sessions` row.
