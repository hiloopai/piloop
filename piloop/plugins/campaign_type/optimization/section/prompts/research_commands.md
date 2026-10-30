- Ideas: `piloop idea add`, `piloop idea status <id> open|dropped|done`, `piloop idea link <id> after <target>`,
  `piloop idea unlink`, `piloop idea base <id> <candidate>|head`,
  `piloop idea track <id> <track>`.
- Candidates: `piloop candidate verdict <c> keep|interesting|discard|crash` when the number is not the full story,
  `piloop candidate remeasure <c>`, `piloop candidate note <c> '<why>'`, `piloop candidate import`, `piloop measurement
  add`, `piloop eval cancel <c>`.
- Runs: `piloop run cancel <run> --wrap-up` ends a redundant run.
- Read: `piloop history` prints the research pack.
