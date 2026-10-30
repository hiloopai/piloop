```sql
-- what is running right now
SELECT r.id, i.title, r.harness, r.model, r.started_at FROM runs r JOIN ideas i ON i.id = r.idea_id
WHERE r.status = 'running';

-- the queue: open ideas the scheduler may take (filter by campaign, which the index serves; every 'after'
-- prerequisite must be done or dropped)
SELECT i.id, i.priority, i.title FROM ideas i WHERE i.campaign_id = <campaign id> AND i.status = 'open' AND NOT EXISTS (
  SELECT 1 FROM idea_links l JOIN ideas p ON p.id = l.target_id
  WHERE l.idea_id = i.id AND l.kind = 'after' AND p.status NOT IN ('done', 'dropped')) ORDER BY i.priority, i.id;

-- open ideas that a prerequisite holds, and what state it is in
SELECT i.id, i.title, p.id AS prerequisite, p.status AS prerequisite_status
FROM ideas i JOIN idea_links l ON l.idea_id = i.id AND l.kind = 'after' JOIN ideas p ON p.id = l.target_id
WHERE i.campaign_id = <campaign id> AND i.status = 'open' AND p.status NOT IN ('done', 'dropped');

-- everything said about idea 12
SELECT author, kind, body, created_at FROM comments WHERE target_kind = 'idea' AND target_id = 12 ORDER BY id;
```
