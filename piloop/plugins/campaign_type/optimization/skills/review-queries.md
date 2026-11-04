## Query a campaign

Everything that piloop did is in its database. `piloop schema` prints every table and its columns for this release;
read it first. A database of another release can differ: run `.schema <table>` in `sqlite3` on it before a query.

```bash
cp <home>/piloop.db* /tmp/review/           # the database with its -wal and -shm, so that nothing writes the original
sqlite3 -header /tmp/review/piloop.db '.tables'
piloop schema
```

The tables that a review reads most:

| Table | What a row is |
|---|---|
| `events` | The feed of what happened: `at`, `target_kind`, `target_id`, and a JSON `payload` whose `$.kind` names the event. Count the kinds first: `SELECT payload->>'$.kind', COUNT(*) FROM events GROUP BY 1`. |
| `candidates` | A version that a run submitted: `run_id`, `parent_id`, `revision`, `origin`. |
| `measurements` | One evaluation of a candidate: `metrics` (JSON), `error`, `seconds`, `evaluator_id` (one evaluator per split). |
| `runs`, `ideas`, `comments` | A run of an idea, the idea, and comments: notes, messages, escalations (`kind`). |
| `agent_sessions` | An agent: its run (`subject_kind`, `subject_id`), `harness`, `model`, `session_id`, `usage`, `state`, `error`. |
| `leases` | A job's claim by a worker: an evaluation's or an agent's, with `created_at`, `started_at`, `ended_at`. |

`<p>` is the campaign id in the queries below. `campaigns.evaluator_id` is the evaluator whose score ranks (the dev
split); another evaluator of the campaign is the sealed split. A measurement's `evaluator_id` says which one it is:
`SELECT id, key FROM evaluators WHERE campaign_id = <p>`.

**The new bests**: a score that made its candidate the best of its track. A score can be written twice as a
`candidate.scored` event: count distinct candidates, never events.

```sql
SELECT MIN(e.at) at, e.target_id candidate, MAX(e.payload->>'$.score') score FROM events e
JOIN candidates c ON c.id = e.target_id
WHERE e.target_kind = 'candidate' AND e.payload->>'$.kind' = 'candidate.scored' AND c.campaign_id = <p>
  AND e.payload->>'$.best' = e.target_id GROUP BY e.target_id ORDER BY at;
```

**The best at a fixed time**, such as 1 h after the first event, and its sealed score (`<sealed>` is the sealed
evaluator's id). Compare arms on this, not on runs of different lengths.

```sql
WITH best AS (SELECT e.target_id c, MIN(e.at) at FROM events e JOIN candidates k ON k.id = e.target_id
              WHERE e.payload->>'$.kind' = 'candidate.scored' AND e.payload->>'$.best' = e.target_id
                AND k.campaign_id = <p> GROUP BY e.target_id)
SELECT b.c, b.at, AVG(json_extract(m.metrics, '$.<metric>')) sealed FROM best b
JOIN measurements m ON m.candidate_id = b.c AND m.evaluator_id = <sealed> AND m.error IS NULL
WHERE b.at <= datetime((SELECT MIN(at) FROM events), '+1 hours') GROUP BY b.c ORDER BY b.at DESC LIMIT 1;
```

A permutation p for two arms' scores, all splits of the runs (exact for a few runs per arm):

```python
from itertools import combinations
from statistics import mean

a, b = [0.358, 0.351, 0.366], [0.376, 0.369, 0.381]  # one score per run
pooled, gap = a + b, abs(mean(a) - mean(b))
splits = list(combinations(range(len(pooled)), len(a)))
hits = sum(abs(mean([pooled[i] for i in s]) - mean([pooled[i] for i in range(len(pooled)) if i not in s])) >= gap for s in splits)
print(f'gap {gap:.3f}, p {hits / len(splits):.2f}, n {len(a)} and {len(b)}')
```

A database of 0.5 has no `candidate.scored` events. Its keeps moved the head, and each verdict is a `candidate.verdict`
event:

```sql
SELECT e.at, e.target_id candidate FROM events e JOIN candidates c ON c.id = e.target_id
WHERE e.target_kind = 'candidate' AND e.payload->>'$.kind' = 'candidate.verdict' AND e.payload->>'$.new' = 'keep'
  AND c.campaign_id = <p> ORDER BY e.id;
```

**Races** (0.5): discards that beat their own parent, because another keep moved the head while they were judged.

```sql
WITH v AS (SELECT target_id c, payload->>'$.new' verdict FROM events WHERE payload->>'$.kind' = 'candidate.verdict'
           AND id IN (SELECT MAX(id) FROM events WHERE payload->>'$.kind' = 'candidate.verdict' GROUP BY target_id)),
     j AS (SELECT target_id c, payload->>'$.parent_delta' parent, payload->>'$.best_delta' best FROM events
           WHERE payload->>'$.kind' = 'candidate.judged')
SELECT COUNT(*) races, (SELECT COUNT(*) FROM v WHERE verdict = 'discard') discards
FROM j JOIN v USING (c) WHERE v.verdict = 'discard' AND j.parent > 0 AND j.best <= 0;
```

**Bests that the final best replaced** rather than built on: a best is in the final best when git says it is an
ancestor. The repo is the campaign's, or `git clone --bare <bundle>` of an export.

```bash
git -C <repo> merge-base --is-ancestor <best revision> <final revision> && echo kept || echo replaced
```

**The evaluation queue**: how long evaluations waited for a worker, and how long they ran. An evaluation's own row
goes once it applied; its lease and the `lease.queued` event on its candidate stay.

```sql
SELECT COUNT(*) n, ROUND(AVG((julianday(l.started_at) - julianday(l.created_at)) * 86400)) mean_wait_s,
       ROUND(MAX((julianday(l.started_at) - julianday(l.created_at)) * 86400)) max_wait_s,
       ROUND(AVG((julianday(l.ended_at) - julianday(l.started_at)) * 86400)) mean_run_s
FROM leases l JOIN events e ON e.payload->>'$.kind' = 'lease.queued' AND e.payload->>'$.lease' = l.id
JOIN candidates c ON c.id = e.target_id AND e.target_kind = 'candidate'
WHERE l.kind = 'evaluation' AND l.started_at IS NOT NULL AND c.campaign_id = <p>;
```

**Rule failures by class**: errors, and guardrails past their floor (`<guard>` and `<floor>` from
`SELECT config->'$.metrics' FROM campaigns`), for each evaluator.

```sql
SELECT m.evaluator_id, COUNT(*) evaluations, SUM(m.error IS NOT NULL) errors,
       SUM(json_extract(m.metrics, '$.<guard>') > <floor>) past_floor
FROM measurements m JOIN candidates c ON c.id = m.candidate_id WHERE c.campaign_id = <p> GROUP BY m.evaluator_id;
SELECT substr(m.error, 1, 80) error, COUNT(*) FROM measurements m JOIN candidates c ON c.id = m.candidate_id
WHERE c.campaign_id = <p> AND m.error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

**Evaluations beside each evaluation**, and how many broke the floor. This counts piloop's evaluations only: the
agents' own builds and runs load the machine too, and they are only in the session files.

```sql
WITH ev AS (SELECT started_at s, ended_at f FROM leases WHERE kind = 'evaluation' AND started_at IS NOT NULL
            AND ended_at IS NOT NULL)
SELECT MIN((SELECT COUNT(*) FROM ev WHERE ev.s < m.created_at
            AND ev.f > datetime(m.created_at, '-' || CAST(COALESCE(m.seconds, 0) AS INT) || ' seconds')) / 5 * 5, 20)
         beside,
       COUNT(*) evaluations, SUM(m.error IS NOT NULL OR json_extract(m.metrics, '$.<guard>') > <floor>) broke
FROM measurements m JOIN candidates c ON c.id = m.candidate_id WHERE c.campaign_id = <p> GROUP BY 1;
```

**Tokens and time by run.** The database has the tokens, not the price: price them by the model's list price.

```sql
SELECT a.subject_id run, a.harness, a.model, COUNT(*) sessions, SUM(a.usage->>'$.turns') turns,
       SUM(a.usage->>'$.input') input, SUM(a.usage->>'$.cache_read') cache_read, SUM(a.usage->>'$.output') output,
       ROUND(SUM(julianday(a.ended_at) - julianday(a.started_at)) * 1440) minutes
FROM agent_sessions a WHERE a.campaign_id = <p> AND a.subject_kind = 'run' GROUP BY a.subject_id ORDER BY run;
```

**Ideas and their notes**: when each run started, and how many notes there were before it and on its idea. Whether
the run read them is in its session file.

```sql
SELECT r.id run, i.title, r.started_at,
       (SELECT COUNT(*) FROM comments n WHERE n.kind = 'note' AND n.created_at < r.started_at) notes_before,
       (SELECT COUNT(*) FROM comments n WHERE n.kind = 'note' AND n.target_kind = 'idea' AND n.target_id = i.id) notes_on_it
FROM runs r JOIN ideas i ON i.id = r.idea_id WHERE r.campaign_id = <p> ORDER BY r.id;
```

**Messages that never reached their agent**, against all the messages, and why. Check this in every review: a harness
that reads messages only between turns never sees a score.

```sql
SELECT (SELECT COUNT(*) FROM comments WHERE kind = 'message') messages,
       (SELECT COUNT(*) FROM events WHERE payload->>'$.kind' = 'comment.undelivered') undelivered;
SELECT e.payload->>'$.error' why, COUNT(*) FROM events e WHERE e.payload->>'$.kind' = 'comment.undelivered' GROUP BY 1;
```

**From submit to told**: how long a worker waited to hear its score.

```sql
WITH s AS (SELECT payload->>'$.candidate' c, MIN(at) at FROM events WHERE payload->>'$.kind' = 'run.submitted' GROUP BY 1),
     t AS (SELECT payload->>'$.candidate' c, MIN(at) at FROM events WHERE payload->>'$.kind' = 'run.score_told' GROUP BY 1)
SELECT COUNT(*) told, ROUND(AVG((julianday(t.at) - julianday(s.at)) * 86400)) mean_s,
       ROUND(MAX((julianday(t.at) - julianday(s.at)) * 86400)) max_s,
       (SELECT COUNT(*) FROM s) submitted
FROM s JOIN t USING (c);
```

**Cross-line parents**: candidates built on another run's candidate, which is how one worker takes up another's line.

```sql
SELECT COUNT(*) candidates, SUM(p.run_id IS NOT NULL AND p.run_id != c.run_id) on_another_run
FROM candidates c JOIN candidates p ON p.id = c.parent_id WHERE c.campaign_id = <p>;
```

**Duplicate ideas**: two ideas with one title, created within a minute: workers that start alike.

```sql
SELECT a.id, b.id, a.title FROM ideas a JOIN ideas b ON b.id > a.id AND b.campaign_id = a.campaign_id
  AND lower(b.title) = lower(a.title) AND ABS(julianday(b.created_at) - julianday(a.created_at)) * 86400 < 60
WHERE a.campaign_id = <p>;
```

**Infra events**, and agents that ended with an error. The daemon's log (`<home>/serve.log`) and a driver's log hold
the rest: `grep -nE 'error=|Traceback|exited [1-9]|137|Killed' <log>`.

```sql
SELECT e.payload->>'$.kind' kind, COUNT(*), MIN(e.at), substr(MAX(e.payload->>'$.error'), 1, 80) FROM events e
WHERE e.payload->>'$.kind' IN ('evaluation.infra_error', 'lease.expired', 'lease.stale_report',
      'agent_session.usage_unknown') GROUP BY 1;
SELECT state, substr(error, 1, 60) error, COUNT(*) FROM agent_sessions WHERE campaign_id = <p> AND error IS NOT NULL
GROUP BY 1, 2;
```

### Session files

An agent's session file is its harness's own transcript. `agent_sessions.session_id` names it, and
`piloop cas get <agent_sessions.session_file>` prints the copy that piloop keeps. On the machine that ran the agent:

| Harness | File |
|---|---|
| Claude Code | `$CLAUDE_CONFIG_DIR/projects/<cwd, non-alphanumerics as ->/<session_id>.jsonl` |
| Codex | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<session_id>.jsonl` |
| pi | `~/.pi/agent/sessions/--<cwd>--/*_<session_id>.jsonl` |

These formats belong to each harness, and they change between its releases. Inspect a file before you trust a recipe
below:

```bash
jq -r '[.type, (.payload.type // .message.role // "")] | join(" ")' <file> | sort | uniq -c | sort -rn | head
```

Codex: each command with its duration and its line, and the tokens of each response.

```bash
jq -c 'select(.payload.type == "item_completed" and .payload.item.type == "CommandExecution") | {line: input_line_number, at: .timestamp, s: .payload.item.duration.secs, cmd: .payload.item.command[-1][0:100]}' <file>
jq -c 'select(.payload.type == "token_count" and .payload.info != null) | .payload.info.last_token_usage' <file>
```

Claude Code: each tool call, each result, the responses (one per `message.id`) and the API errors.

```bash
jq -c 'select(.type == "assistant") | .timestamp as $t | .message.content[]? | select(.type == "tool_use") | {at: $t, id, name, what: ((.input.command // .input.file_path // "") | tostring | .[0:100])}' <file>
jq -c 'select(.type == "user") | .timestamp as $t | .message.content[]? | select(type == "object" and .type == "tool_result") | {at: $t, id: .tool_use_id}' <file>
jq -s '[.[] | select(.type == "assistant" and .message.usage) | .message.id] | unique | length' <file>
jq -c 'select(.isApiErrorMessage == true) | {at: .timestamp, text: .message.content[0].text}' <file>
```

To cite a line of a session file, take it from `input_line_number`, and read it again with `sed -n '<n>p' <file>`.
