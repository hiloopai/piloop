---
name: piloop-review
description: Explain why a piloop campaign turned out the way it did - why a result is good or bad, why the evaluation or the infra failed, how good ideas were found or why not, why one worker or arm did better, and where time went to waste - and tie each cause to the part of piloop that owns it. Use when the human asks to review, audit, post-mortem or compare campaigns or arms, or asks if a result can be trusted.
---

# Review a piloop campaign

A review explains why a campaign run turned out the way it did, and names the part of piloop that owns each cause.
Subagents answer six fixed questions from the database, the agents' session files and the logs. Each number comes with
the command that produced it, and a verifier runs every command again.

The data, and worked queries on it, are in the last part of this skill, "Query a campaign".

## The questions

Every review answers these, in this order:

1. Why is the result good, or bad?
2. If the evaluation is bad, why?
3. If there were infra failures, why?
4. Were good ideas found? How, or why not?
5. If another worker or arm did better, why?
6. Where did time go to waste?

## Rules

- Read only. Copy a database before you query it (with its `-wal` and `-shm` files), or open it with `?mode=ro`. Never
  prompt a running agent, edit a campaign repo or change config.
- **Every number in a claim carries the exact command that produced it**: a SQL statement with the database it ran on,
  or a shell, `jq` or `git` command line, and the part of its output that holds the number. The claim's own commands
  print each of its numbers: a number that another claim printed does not count. A number without its command goes,
  and so does a claim that is left with none.
- Tag each claim **M** (a command measured it) or **I** (you reasoned to it from measured claims). An agent's own
  report, in its session or its notes, is a claim to check, not evidence.
- **Compare like with like.** Before you compare arms, check that they share the piloop pin, the base revision, the
  task's rules and the platform (an eval run's `run.json`, or each campaign's config and repo). Name arms that differ
  as different, and do not compare them.
- **Compare at a fixed time**, such as 1 h, on the sealed score of the best at that time. Give each arm's mean and
  range and a permutation p, and say when an arm has fewer than 3 runs.
- Each role writes only under its own `work/<role>/`. Snapshot a live file (a log, a session file of a running agent)
  into it before a claim reads it.
- Explore before you trust a recipe: a database of another piloop release, and every session file, may differ from
  the examples. Look at the schema and at a few records first.
- Say what the review will cost before it starts. A run's review stays under $10 unless the human allows more.
- Bug reports are drafts in files. File nothing, post nothing.

## The roles

Run each role as a read-only subagent, in parallel. Give each one this skill, the paths of the run's data, and its
share of the questions.

| Role | Reads | Answers |
|---|---|---|
| **Reader**, one per worker (past 4 workers, one per 2–3 workers) | the worker's runs, candidates and session files: the turns around its biggest gains, its longest waits and its failed evaluations | 1, 4, 6 for its workers |
| **Comparer** | every worker, and the other runs of an arm or wave, with a lone agent or plain arm beside the piloop arms | 5 |
| **Eval auditor** | measurements and their errors, rule failures, an outside score if there is one, the diffs of the bests, the integrity checks below | 2, and whether the result is real |
| **Infra auditor** | infra events, killed agents, the daemon log, any driver log; the provider's record of how each box ended (for Modal, the stored result's status and exception: `poll()` reads 137 for our own terminate too); live probes and memory traces; outcomes counted by box config | 3 |
| **Verifier** | every claim of the other roles | runs each command again, and checks each number against its output |
| **Synthesizer** | the verified claims | maps each cause to its owner and a change |

Each role returns its claims as JSON lines, one per claim:

```json
{"question": 1, "run": "10w", "claim": "451 of 501 discards were races", "owner": "judge/pool", "change": "judge a candidate against its parent", "evidence": [{"command": "sqlite3 10w.db < races.sql", "output": "451|501"}], "tag": "M"}
```

**A reader** whose workers left no session files says so, and reads the database and the repo only. Merge readers
rather than leave one with nothing to read.

**The infra auditor** may run a repro under $3 when it separates two causes, and says what it cost.

**The comparer** always sets a lone-agent or plain arm, when the wave has one, beside the piloop arms, on the same
measures: the same queries and recipes, run on each arm. A measure that one arm lacks, such as a database, is said, not
skipped.

**The verifier** runs every `command` of every claim again, on the same data. Then it checks each number of `claim`
against what the commands printed, one by one: the number is printed, or it follows from printed numbers by a step the
claim names (a sum, a share, a difference, a rounding). A command that runs is not enough. It rejects a claim when a
command fails, when its output differs from `output`, or when one number of the claim is in no output. It returns the
claims that pass, and for each rejected one the number or the command, and what was printed. The synthesizer uses
only claims that passed.

## The owners

The synthesizer gives every cause one owner, by the counterfactual: if this were fixed, would the outcome change?

| Owner | Typical causes |
|---|---|
| **brief** | what the agents are told: an instruction that makes them wait, start alike, or skip notes |
| **judge/pool** | how submissions are scored and ranked: races, a bar that moves, a rule that counts noise |
| **evaluator** | how an evaluation runs: on whose CPUs, how long, how often again |
| **scheduler** | when runs start, how many at once, on what base |
| **harness** | the coding-agent CLI: messages it drops, a session that ends early, tokens it rereads |
| **eval task** | the task's own rules and data: a rule the agents never saw, a split unlike the holdout |
| **infra** | machines, memory, quotas, gateways |
| **model** | what the model did with all the above right |

Separate infra and harness faults from the model's before you blame the model.

## Integrity checks (eval auditor)

- **Frozen files.** `git diff --stat <baseline> <best> -- <evaluate> <data>` for each best. An edited `evaluate.py`
  scores with no warning: the evaluator key does not hash its contents.
- **Labels and references.** Search the session files for reads of the labels, the references or per-unit results.
- **Tuning against the evaluation.** Count each worker's evaluate calls in its session files, and the daemon's
  evaluations. Many looks at a small panel is a screen, not a judge.
- **Transfer.** Compare the campaign's score with an outside one, where there is one. A best that an outside scoring
  refuses breaks a rule that the campaign did not check.

## The output

- **A report in markdown**, by default: three lines first (what ran, the result, the main why), then the cause table:
  question, claim, owner, change, M or I, the command behind each number. Put long evidence in an appendix.
- **A shared page** instead, when the human asks for one, following their own page skill if they have one.
- **One report per arm or wave**: a question × run matrix, and the causes counted by owner.
- **Bug drafts**: one file per cause owned by piloop's own code, with its commands and a minimal reproduction.
