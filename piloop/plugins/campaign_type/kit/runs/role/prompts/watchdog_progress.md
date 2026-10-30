## This sweep: a progress review

Is the campaign really getting closer to the goal, at a fair cost? Read diffs, transcripts, notes and the
measurements behind the numbers.

- **Real?** Does the gain serve the brief's goal, or only the metric? Is it concentrated in a few units, or tuned to the
  scored panel?
- **Efficient?** Tokens and evaluation time per improvement, wasted work, idle slots, runs that end without a scored
  version, and which sources' ideas keep.
- **Stuck?** Time and runs since the last trunk keep, a track nobody feeds, a mechanism tried again.
- **What would you change?** A direction, the config or the brief, or stopping.

Write one `note` on the campaign that starts with your verdict (on track, inefficient, stuck or suspect), then the
evidence. Make it an `escalation` when the orchestrator should change course now.

When the trigger says that the campaign stopped, this is the closing review: the head and its lineage, what worked,
what did not, whether to trust the result, and one escalation that asks whether to extend, change or finish. Then
leave one row of `lessons` for each family that this campaign tried, where it says something new beside the lessons
already on this repo ("Remember past campaigns"):

```sql
INSERT INTO lessons (campaign_id, family, status, effect, metric, body, created_by)
VALUES (<campaign id>, '<family>', 'dead', 0.002, '<metric>', '<what was tried, why it stopped, with ids>', '<you>');
```

`status` is `dead`, `promising` or `untested`. `effect` and `metric` are NULL when nothing was measured. A lesson is
never changed: a new conclusion is a new row.
