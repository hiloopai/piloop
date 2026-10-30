## Commits

- Commit in your worktree. After your run ends, the daemon scores every commit with the campaign's evaluate command
  and judges it. Nothing is scored while you work: do not wait for a score.
- Commit each step that runs. Each commit is scored on its own.
- A commit that scores worse but opens a direction worth following: end its message with the trailer
  `Piloop-Note: <why>`. Its candidate is then `interesting`, not a failure.
- `Piloop-Eval: skip` as a trailer skips the evaluation of a commit that cannot run at all. Never use it to avoid a
  score.
- What you leave uncommitted is committed for you as `recovered` work: never measured, never kept. Commit the tree you
  would ship. A run that saved nothing has no result.
- Before you build a mechanism from scratch, read how earlier candidates did it: `git show <revision>`, with
  `SELECT revision FROM candidates WHERE id = <n>`.
