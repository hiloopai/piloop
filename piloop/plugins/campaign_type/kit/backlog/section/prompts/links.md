Links (`idea_links`) relate ideas of one campaign:

- `after`: the idea waits for its prerequisites. It stays `open` but is held until each of them is `done` or
  `dropped`. Then it starts from the head, which has what they kept.
- Link a new idea when you write it: `--after <n>` on `piloop idea add`. The idea and its links are written together.
- Never add an `open` idea and then insert its `after` link: the daemon can start the idea in between.
