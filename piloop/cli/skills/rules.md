## Rules

- Never change `runs.status` or `events`. Never change `agent_sessions`, except to insert a request.
- If they look wrong, record what you see in a `note` on the run, and fix it through a piloop command. When no
  command fixes it, escalate to the human.

Find who changed what:

```sql
SELECT at, actor, kind, payload FROM events WHERE target_kind = '<k>' AND target_id = <t> ORDER BY id
```

- Changes to an idea's status, priority or track, a candidate's verdict and a campaign's status or pause carry
  `{old, new}` and the actor (`human`, `orchestrator`, `daemon`, `watchdog#7` …).
- `idea.linked` and `idea.unlinked` carry the target and the kind.
  The commands put their reason in a note on the idea. `measurement.recorded` has the metrics.
- Deletes of ideas, comments and candidates leave `*.deleted` tombstones.
