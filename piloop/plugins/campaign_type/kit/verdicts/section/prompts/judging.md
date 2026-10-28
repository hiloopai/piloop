The daemon judges every new candidate against the bar on its track. The bar is the last kept candidate of the run,
starting at its base. The daemon writes why in `verdict_reason`:

- `keep`: the track's primary beat the bar with confidence, and every guardrail (other tracks' primaries included)
  stayed within its margin.
- `interesting`: the primary is up but not significantly, or a guardrail regressed beyond its margin.
- `discard`: the primary is not up, or a guardrail passed its floor.
- `crash`: every measurement failed.

Special reasons:

- A reason that starts with `no-op:` means the primary and the guardrails equal the parent's exactly.
- `cause = 'moved'` is a keep that was rebased: it kept against its own bar, but the head moved on. The reason names
  the merged candidate or the conflict.
- `cause = 'note'` is a regressed result that an agent kept in view as a lead.
- A `skipped` version asked not to be evaluated (scaffolding). It has no measurement and says nothing about the idea.

Roles, tracks, margins and floors are in `campaigns.config`.

A candidate's score:

```sql
-- a candidate's score under the campaign's current evaluator (mean over its full-panel measurements);
-- a ratio metric of [[metrics]] is not stored: divide the means of its parts
SELECT c.id, c.verdict, c.summary, AVG(json_extract(m.metrics, '$.<primary metric>')) AS score, COUNT(m.id) AS n
FROM candidates c JOIN campaigns p ON p.id = c.campaign_id
LEFT JOIN measurements m
  ON m.candidate_id = c.id AND m.evaluator_id = p.evaluator_id AND m.error IS NULL AND m.rung IS NULL
WHERE c.campaign_id = <campaign id> GROUP BY c.id ORDER BY c.id DESC LIMIT 20;
```
