## Pause or stop a campaign

```bash
PILOOP_ACTOR=orchestrator piloop pause <name> --reason '<what the pause waits for>'
PILOOP_ACTOR=orchestrator piloop resume <name>
```

- Nothing launches: no run, no sweep, no requested agent. Research files no ideas. Running work finishes and is
  scored.
- A pause raises no escalation. `piloop status` shows it with the reason.
- No agent launches, the incident watchdog included: an anomaly of the running work waits in `piloop status` until the
  resume. Watch the running work yourself during a long pause. Paused time does not count toward `max_hours`.
- Never pause with a `hold_at` fact that you set by hand, nor with a gate idea and `after` links: a hold stops every
  measurement, and research files ideas without the link.
- Pausing a campaign is a scope decision for the human: record it (see "Every decision you take for the human").
- A campaign: `piloop campaign stop <name> --reason '<why>'`. The daemon stops it as a limit of the stop rule does: it
  drops queued work, winds the agents down, then launches the closing review. `--kill` ends the agents at once. Never
  `UPDATE campaigns SET status`: that gets no closing review.
- An agent that became redundant: `piloop agent cancel <agent-session> --wrap-up`. The agent is told to save its work
  and stop, and ended 10 minutes later, with every process it started. Without `--wrap-up`, it is ended at once.
- Never `pkill` an agent: the cancel ends what it left running, in any session.
