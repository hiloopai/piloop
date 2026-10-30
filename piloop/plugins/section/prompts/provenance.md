- Every table except `events` has `updated_at` and `updated_by`.
- Triggers set both on every UPDATE. `updated_by` is set to whoever holds the connection (`human`, `orchestrator`,
  `daemon`, `research#12` …), so a value you write there is overwritten.
- Never set them by hand. Read them to see who changed a row and when. `updated_by IS NULL` means the row is unchanged
  since its insert.
- The same triggers keep a change log in `events`, signed by the same actor. Each payload has its `kind`, and these have
  `old` and `new`:
  `idea.status`, `idea.priority`, `idea.track`, `candidate.verdict` (with `verdict_by` and
  `verdict_reason`), `candidate.origin`, `run.brief_cid` (the old and new brief's CID), `campaign.status` (with `stopped_reason`), `campaign.paused` (with
  `paused_reason`). An inserted or deleted `idea_links` row is `idea.linked` or `idea.unlinked` on its idea, with the
  target and the kind.
- A DELETE of an idea, comment or candidate leaves a `<kind>.deleted` event with the row's key columns.
- So `SELECT at, actor, kind, payload FROM events WHERE target_kind = 'idea' AND target_id = 12 ORDER BY id` is the full
  history of an idea.
- The daemon's own `candidate.judged` event is next to `candidate.verdict`. It has the score that the verdict was made
  from.
- A note on a candidate is a `candidate.noted` event, signed by its author, with its reason. `verdict_by` changes only
  with the verdict.
