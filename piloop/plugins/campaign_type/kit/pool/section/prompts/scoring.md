Every scored candidate is in the campaign's pool. Nothing is kept or discarded:

- The daemon evaluates a submission once. When its score beats the best's, the daemon evaluates it again until it has
  `[evaluator] noise_runs` evaluations (it is **confirmed**), and scores it on the sealed split, if the campaign has one.
- The **best** of a track is the confirmed candidate with the best mean of the track's primary that the best rule
  allows, else the baseline. By default, the rule allows a candidate that beats the baseline on the sealed split and
  keeps each guardrail within its margin and floor of the baseline.
- A candidate's **line** is its chain of parents. A run starts from the best, or from any candidate that its claim
  names with `--base <candidate>`.
- `excluded_reason` says why the orchestrator or a person took a candidate out of the race for the best.
- A `skipped` version is one that its run did not submit. It has no measurement and says nothing about the idea.

Roles, tracks, margins and floors are in `campaigns.config`.

A candidate's score:

```sql
-- a candidate's score under the campaign's current evaluator (mean over its full-panel measurements);
-- a ratio metric of [[metrics]] is not stored: divide the means of its parts
SELECT c.id, c.summary, AVG(json_extract(m.metrics, '$.<primary metric>')) AS score, COUNT(m.id) AS n
FROM candidates c JOIN campaigns p ON p.id = c.campaign_id
LEFT JOIN measurements m
  ON m.candidate_id = c.id AND m.evaluator_id = p.evaluator_id AND m.error IS NULL AND m.rung IS NULL
WHERE c.campaign_id = <campaign id> GROUP BY c.id ORDER BY score DESC LIMIT 20;
```
