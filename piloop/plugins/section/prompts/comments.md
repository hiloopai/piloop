- `INSERT INTO comments (target_kind, target_id, author, kind, body)`, piped into `piloop sql -`.
- Refer to other rows in the body as `idea#12`, `run#7`, `candidate#30`.
- Kinds: `note`, `finding`, `decision`, `escalation`. A lesson is a row of `lessons` ("Remember past campaigns").
- Every comment also inserts a `comment.<kind>` event (a trigger).
- A comment on an idea, or on any of its runs, goes into the brief of the idea's next run.
- A brief shows other comments by id and first line, so put the gist in the first line. `SELECT body FROM comments
  WHERE id = <n>` reads the full text.
- `comment.escalation` reaches the human through the orchestrator. Use `escalation` only when a human decision is
  needed.
- An escalation must have a priority, from 0 (urgent) to 4. A CHECK constraint refuses it otherwise:
  `INSERT INTO comments (target_kind, target_id, author, kind, body, priority) VALUES ('run', 7, '<you>', 'escalation',
  '<what happened, the evidence, the one question>', 1)`.
- Never change `runs`, `measurements` or `agent_sessions` rows: the daemon owns them. If one looks wrong, write an
  `escalation` on the run. Only piloop writes `events`.
