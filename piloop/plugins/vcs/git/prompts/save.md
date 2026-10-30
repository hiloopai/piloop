## Commits

- Commit in your worktree, each step that runs. "Measure a change" says which commits piloop scores, and when.
- What you leave uncommitted is committed for you as `recovered` work: never measured, never kept. Commit the tree you
  would ship. A run that saved nothing has no result.
- Only when piloop scores every commit after your run: end the message of a commit that scores worse but opens a
  direction with the trailer `Piloop-Note: <why>`, and its candidate is `interesting`, not a failure. Mark a commit that
  cannot run at all with `Piloop-Eval: skip`, never one that you want to keep from a score.
- Before you build a mechanism from scratch, read how earlier candidates did it: `git show <revision>`, with
  `SELECT revision FROM candidates WHERE id = <n>`.
