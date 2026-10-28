Links (`idea_links`) relate ideas of one campaign:

- `after`: the idea waits for its prerequisites. It stays `ready` but is not taken until all of them are `done`. Then
  it starts from the head, which has what they kept.
- `supersedes`: the idea is a sharper version of older ideas. The daemon rejects the older ones that never ran.
- Link a new idea when you write it: `--after <n>` and `--supersedes <n>` on `piloop idea add` or `piloop idea
  develop`. The idea and its links are written together.
- Never add a `ready` idea and then insert its `after` link: the daemon can start the idea in between.
