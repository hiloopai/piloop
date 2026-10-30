- `INSERT INTO comments (target_kind, target_id, author, kind, body)`, piped into `piloop sql -`.
- Refer to other rows in the body as `idea#12`, `run#7`, `candidate#30`.
- Kinds: `note`, `finding`, `decision`. A lesson is a row of `lessons` ("Remember past campaigns").
- Every comment also inserts a `comment.<kind>` event (a trigger).
- A comment on an idea, or on any of its runs, goes into the brief of the idea's next run.
- A brief shows other comments by id and first line, so put the gist in the first line. `SELECT body FROM comments
  WHERE id = <n>` reads the full text.
- An escalation asks the human for a decision, through the orchestrator: file it with `piloop escalate <target>
  --priority <0 to 4>`, its form as JSON on stdin (`--guide` shows the form). An INSERT of one is refused.
- Never change `runs`, `measurements` or `agent_sessions` rows: the daemon owns them. If one looks wrong, escalate on
  the run. Only piloop writes `events`.
