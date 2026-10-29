`workspace/` is a copy of the campaign's shared workspace as it was when your run started. Its files are read-only:
to change one, remove it and write it again. What you write there goes into the shared workspace when your run ends.

- `workspace/NOTES.md` and `workspace/research/` stay. Your idea's directory is archived when the idea is done.
- Put what you produce in your idea's directory, large intermediate arrays included, never at the top level.
- Never copy the code tree into it.
- When you run the evaluator yourself, leave its output where the campaign's evaluate writes it (its own cache, keyed
  by tree and shared by every run). Never redirect it into the workspace.
