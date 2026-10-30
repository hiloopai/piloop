Every campaign that ever ran is in this database. `campaigns.repo` names the code that each campaign worked on, the
same for every clone. Before you propose or build a mechanism, look at what came of it before.

Prior attempts on this repo, in every campaign, with what they came to and the primary metric that judged them. A
discard under one campaign's evaluator is not dead under another's: read the idea's comments before you call it
settled.

```sql
SELECT p.name AS campaign, i.id, i.status, i.title,
  (SELECT group_concat(DISTINCT c.verdict) FROM runs r JOIN candidates c ON c.run_id = r.id WHERE r.idea_id = i.id) AS verdicts,
  (SELECT group_concat(json_extract(m.value, '$.name')) FROM json_each(p.config, '$.metrics') m
   WHERE json_extract(m.value, '$.role') = 'primary') AS judged_on
FROM ideas_fts JOIN ideas i ON i.id = ideas_fts.rowid JOIN campaigns p ON p.id = i.campaign_id
WHERE ideas_fts MATCH '"sync acquisition" OR onset' AND p.repo = (SELECT repo FROM campaigns WHERE id = <campaign id>)
ORDER BY bm25(ideas_fts) LIMIT 20;
```

- An implementation to copy, from any campaign: drop the `p.repo` filter, keep what kept or was noted (`JOIN runs r ON
  r.idea_id = i.id JOIN candidates c ON c.run_id = r.id WHERE c.verdict IN ('keep', 'interesting')`) and select
  `p.path, c.revision`. {vcs:show}
- How a past idea was worked: `SELECT w.id, w.session_file, w.log_dir FROM agent_sessions w JOIN runs r ON w.subject_kind = 'run'
  AND r.id = w.subject_id WHERE r.idea_id = <idea>`. `piloop cas get <session_file>` prints the harness's own file of the session.
  `<log_dir>/transcript.txt` is its screen. An archived workspace can be
  restored with `piloop restore`, once your piloop has it.

Lessons: a row of `lessons` is the standing conclusion about one mechanism family, written when a campaign stops.
The newest for each (campaign, family) is the current one. What is dead or promising on this repo:

```sql
SELECT p.name AS campaign, l.id, l.family, l.status, l.effect, l.metric
FROM lessons l JOIN campaigns p ON p.id = l.campaign_id
WHERE p.repo = (SELECT repo FROM campaigns WHERE id = <campaign id>)
  AND l.id = (SELECT MAX(n.id) FROM lessons n WHERE n.campaign_id = l.campaign_id AND lower(n.family) = lower(l.family))
ORDER BY l.status, l.family;
```

- `SELECT body FROM lessons WHERE id = <n>` reads why, with the ids it cites.

Documents: campaign reports, logs and design notes (`[plugins.memory] documents` in `piloop.toml`), searched in
`documents_fts` (over title and body) as in "Search":

```sql
SELECT p.name AS campaign, d.id, d.path, snippet(documents_fts, 1, '[', ']', '…', 12) AS hit
FROM documents_fts JOIN documents d ON d.id = documents_fts.rowid JOIN campaigns p ON p.id = d.campaign_id
WHERE documents_fts MATCH '"clock recovery" OR acquisition' AND p.repo = (SELECT repo FROM campaigns WHERE id = <campaign id>)
ORDER BY bm25(documents_fts) LIMIT 10;
```

- `SELECT body FROM documents WHERE id = <n>` reads one. It can be long.
