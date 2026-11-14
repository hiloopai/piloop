Every campaign that ever ran is in this database. `campaigns.repo` names the code that each campaign worked on, the
same for every clone. Before you propose or build a mechanism, look at what came of it before.

Prior attempts on this repo, in every campaign, with their best score and the primary metric that scored them. A
low score under one campaign's evaluator is not dead under another's: read the idea's comments before you call it
settled.

```sql
SELECT p.name AS campaign, i.id, i.status, i.title,
  (SELECT count(DISTINCT s.candidate_id) FROM submissions s WHERE s.idea_id = i.id) AS submitted,
  (SELECT group_concat(json_extract(m.value, '$.name')) FROM json_each(p.config, '$.metrics') m
   WHERE json_extract(m.value, '$.role') = 'primary') AS judged_on
FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid JOIN campaigns p ON p.id = i.campaign_id
WHERE ideas_fts MATCH '"sync acquisition" OR onset' AND p.repo = (SELECT repo FROM campaigns WHERE id = <campaign id>)
ORDER BY bm25(ideas_fts) LIMIT 20;
```

- An implementation to copy, from any campaign: drop the `p.repo` filter, join what was submitted for it (`JOIN
  submissions s ON s.idea_id = i.id JOIN candidates c ON c.id = s.candidate_id`), and select `p.path, c.revision`. A
  version that a run kept for the idea and never submitted has `c.idea_id = i.id`. {vcs:show}
- How a past idea was worked: `SELECT w.id, w.session_file, w.log_dir FROM agent_sessions w JOIN run_claims c ON
  w.subject_kind = 'run' AND c.run_id = w.subject_id WHERE c.idea_id = <idea>`. `piloop cas get <session_file>` prints the harness's own file of the session.
  `<log_dir>/transcript.txt` is its screen. An archived workspace can be
  restored with `piloop restore`, once your piloop has it.
