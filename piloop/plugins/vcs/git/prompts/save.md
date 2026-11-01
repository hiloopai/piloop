## Commits

- Commit in your worktree, each step that runs. "Measure a change" says which commits piloop scores, and when.
- What you leave uncommitted is committed for you as `recovered` work: never measured, never kept. Commit the tree you
  would ship. A run that saved nothing has no result.
- Before you build a mechanism from scratch, read how earlier candidates did it: `git show <revision>`, with
  `SELECT revision FROM candidates WHERE id = <n>`.
