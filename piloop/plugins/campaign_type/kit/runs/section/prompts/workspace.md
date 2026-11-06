`workspace/` is a copy of the campaign's shared workspace as it was when your run started. Its files are read-only:
to change one, remove it and write it again. What you write there goes into the shared workspace when your run ends,
and into the checkout of every agent after you.

- `workspace/NOTES.md` and `workspace/research/` stay. Your idea's directory is archived when the idea is done.
- Put what a later run needs from yours in your idea's directory, never at the top level.
- Put what a run can make again in `scratch/`, such as samples of the data and the outputs of builds and of your own
  dev runs. It is yours alone, in no version, and it is deleted when your run ends.
- Never copy the code tree into the workspace.
- When you run the evaluator yourself, leave its output where the campaign's evaluate writes it (its own cache, keyed
  by tree and shared by every run). Never redirect it into the workspace.
