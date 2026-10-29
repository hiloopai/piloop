## Read a candidate or a session

- The commit is in the repo:
  - `SELECT revision, summary FROM candidates WHERE id = <c>`, then `git show <revision>`.
  - `git diff <parent revision> <revision>` compares it with its parent (`candidates.parent_id`).
  - `git cherry-pick <revision>` takes it.
  - The kept lineage in `piloop history` lists the commit of each candidate.
- Why it was made: read the session of the agent that made it, the harness's own file:
  `piloop cas get <agent_sessions.session_file>`, for any agent, sweeps included. It is long: read it only for a
  specific question.

## Past research

Every campaign that ever ran is in the database. `campaigns.repo` names the code that each campaign worked on, the same
for every clone. Before you add an idea, and when an agent asks whether something was tried, query it. `piloop
schema past_campaigns` has every query: implementations to copy from any campaign, documents, the
session that worked a past idea.

- Prior attempts on this repo, in every campaign, with what judged them. A discard under one evaluator is not dead
  under another: `SELECT p.name, i.id, i.status, i.title FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid JOIN
  campaigns p ON p.id = i.campaign_id WHERE ideas_fts MATCH '<mechanism>' AND p.repo = (SELECT repo FROM campaigns WHERE
  id = <p>) ORDER BY bm25(ideas_fts) LIMIT 20`.
- Lessons: the standing conclusion about each mechanism family, written by the closing watchdog when a campaign stops.
  The newest for each campaign and family is the current one, and `piloop wake` lists them. You may write one too: a
  row of `lessons` with the family, the status (`dead`, `promising` or `untested`), the best effect and its metric
  (both NULL when none was measured), and a body that says why, citing ids.
- Documents: the reports and logs that `[plugins.memory] documents` in `piloop.toml` names, copied in on `piloop init` and
  when the campaign stops. Search them with `documents_fts MATCH '<words>'`. Point the globs at reports, never at
  anything derived from validation or test data.
- Briefs of implementers, research and the scout show the current lessons of the repo: the `lessons` section of
  `[plugins.role.<kind>] sections`, in its `budget` of tokens. Leave the entry out to turn it off for a role.

## Judge candidates

The daemon's verdict is mechanical:

- `keep`: the candidate's primary metric beat the bar with confidence, and the guardrails held.
- `interesting`: it gained, but without that confidence, or a guardrail went beyond its margin.
- `discard`: otherwise.
- `crash`: it failed to measure.

The bar:

- The bar is the run's last keep, or its base, on the track of its idea.
- With several tracks, the primary metric of every other track is one more guardrail.
- A keep against a bar that the head has passed is `interesting` with the cause `moved`. The daemon merges it onto the
  head and judges it there.
- If that merge conflicts, the daemon files a seed `Compose candidate#N onto the head`.
- The floors and margins of a track change only through an edit to `piloop.toml`. That is a decision: record it.

Override the verdict when the number is not the full story:

```bash
PILOOP_ACTOR=orchestrator piloop candidate verdict <c> interesting --reason '<what the number does not say, with ids>'
```

- The daemon never overwrites a verdict that it did not set.
- `keep|interesting|discard|crash`, and only over a judgement: a pending candidate, and a `skipped` or `recovered`
  one, is refused. A `reference` takes only a keep, once it is measured. A keep from an older bar would drop what the head kept, so piloop merges it onto the head
  instead: the candidate is `interesting`, and the merge is a new candidate that the daemon measures and judges against
  the head. The output names it, or the seed that composes a merge that conflicts.
- A verdict that replaces the head cancels the open runs that started from the old head, and their ideas run again
  from the new head. The output lists them.
- You measured it yourself (the daemon's evaluation timed out, say): `piloop measurement add <c> --report <file>`,
  with `{"metrics": {"<name>": <value>}, "seconds": <s>}` in the file. The daemon judges its `crash`, `discard` or
  `interesting` again with every measurement; the output says when. Never `INSERT INTO measurements`: nothing judges
  it.
- A suspicious jump: `piloop candidate remeasure <c>`. The daemon measures it 3 times next to its bar and judges its
  `crash`, `discard` or `interesting` again on the pairs. A time that load inflates needs `repeats` on its metric in
  `piloop.toml`, not a remeasure or a larger margin.
- A `crash` that the host caused (a full disk, a lost GPU, a load that hit `evaluate_timeout`): `piloop candidate
  remeasure <c>`, after you raise the timeout and run `piloop init`. Never queue an idea with a no-op change to get a
  new tree measured.
- After a new evaluator, the daemon measures only the head and the bars that runs need. An older keep keeps its old
  score until you ask for it with `piloop candidate remeasure <c>`.
- Evaluations wait for a worker's slot (`evaluations` of its pool in `[plugins.executor.pools]`): `piloop eval queue`
  lists them. `piloop eval cancel <c>` cancels one that is no longer worth its slot; the candidate becomes a `crash`
  that names you. Never kill an evaluator process by hand.
