## Read a candidate or a session

- `piloop candidate show <c>` prints where it stands, its metrics beside its parent's, and each rung it measured.
- The commit is in the repo:
  - `SELECT revision, summary FROM candidates WHERE id = <c>`, then `git show <revision>`.
  - `git diff <parent revision> <revision>` compares it with its parent (`candidates.parent_id`).
  - `git cherry-pick <revision>` takes it.
  - The new bests in `piloop history` list the commit of each one.
- Why it was made: read the session of the agent that made it, the harness's own file:
  `piloop cas get <agent_sessions.session_file>`, for any agent. It is long: read it only for a
  specific question.

## Past research

Every campaign that ever ran is in the database. `campaigns.repo` names the code that each campaign worked on, the same
for every clone. Before you add an idea, and when an agent asks whether something was tried, query it. `piloop
schema past_campaigns` has every query: implementations to copy from any campaign, the session that worked a past
idea.

- Prior attempts on this repo, in every campaign. A low score under one evaluator is not dead under another:
  `SELECT p.name, i.id, i.status, i.title FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid JOIN
  campaigns p ON p.id = i.campaign_id WHERE ideas_fts MATCH '<mechanism>' AND p.repo = (SELECT repo FROM campaigns WHERE
  id = <p>) ORDER BY bm25(ideas_fts) LIMIT 20`.

## Read the pool

Every scored candidate stays in the campaign's pool. The daemon scores it, and decides nothing else:

- It evaluates a submission once. When that score beats the best's mean, it evaluates it again until it has
  `[evaluator] noise_runs` evaluations (one when the evaluator is deterministic): it is **confirmed**. With a sealed
  split, it scores it there too.
- The **best** of a track is the best mean of the track's primary among the baseline and the confirmed candidates
  that the best rule allows. It is a query: a new measurement can move it.
- The default best rule, `baseline`, refuses a candidate with a metric past its floor, a guardrail worse than the
  baseline's by more than its `margin` or `fraction`, or, with a sealed split, no win over the baseline there. With
  several tracks, the primary of every other track is one more guardrail.
- The floors and margins of a track change only through an edit to `piloop.toml`. That is a decision: record it.
- The "standing" column of `piloop status` says where each candidate is: `best`, `#2 of 5` (it may be best),
  `3 evals` (not confirmed), `refused` (by the best rule), `excluded`, `crash`, `stopped` (at a rung of the ladder).
- What the worker heard is the `told` of the candidate's latest `candidate.scored` event, for example
  `bytes 2720458 over 1 eval: the best of the bytes track now, rank 1 of 3; vs parent +968317`. A delta that the noise
  can explain says `(within noise)`.

A candidate's **line** is its chain of parents. A run starts from the best, unless its worker chose another base with
`piloop idea claim <id> --base <candidate>`. The trajectory of `piloop status` lists the other lines worth branching
from. To send a run down a line, note its candidate with a follow-up (`piloop candidate note`, above).

## Exclude a candidate

Exclude a candidate whose number is not the full story: it gamed the evaluation, read labels, or broke a rule that the
evaluator does not check.

```bash
PILOOP_ACTOR=orchestrator piloop candidate exclude <c> --reason '<what the number does not say, with ids>'
```

- Its scores stay in the pool, and a run may still start from it. It may not be best, so the best moves to the next.
- The baseline may always be best: piloop refuses to exclude it.
- To take it back: `UPDATE candidates SET excluded_reason = NULL, excluded_by = NULL WHERE id = <c>`, with a note
  that says why.

## Measure again

- You measured it yourself (the daemon's evaluation timed out, say): `piloop measurement add <c> --report <file>`,
  with `{"metrics": {"<name>": <value>}, "seconds": <s>}` in the file. Its mean includes it at once, and the output
  says where it stands. Never `INSERT INTO measurements`.
- A suspicious jump: `piloop candidate remeasure <c>`. The daemon evaluates it 3 more times (`--repeats`), and the
  best follows the new mean. A run's candidate whose mean now beats the best's is confirmed.
- A `crash` that the host caused (a full disk, a lost GPU, a load that hit `evaluate_timeout`): raise the timeout, run
  `piloop init`, then `piloop candidate remeasure <c>`. Never queue an idea with a no-op change to get a new tree
  measured.
- After a new evaluator, the daemon measures the baseline and the best of the old evaluator. Every other candidate
  keeps its old score until you ask for it with `piloop candidate remeasure <c>`.
- Evaluations wait for a worker's slot (`evaluations` of its pool in `[plugins.executor.pools]`): `piloop eval queue`
  lists them. `piloop eval cancel <c>` cancels one that is no longer worth its slot. Never kill an evaluator process by
  hand.
  - The candidate keeps its origin and its scores.
  - The cancel stands under that evaluator until `piloop candidate remeasure <c>`: until then the daemon does not
    confirm the candidate, score it on the sealed split, or measure it again as the baseline or the best. So never
    cancel the confirmation of a candidate that may be the best.
