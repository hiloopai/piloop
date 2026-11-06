# Find and fix the bugs in piloop

This repo is piloop, an autoresearch daemon (`README.md`, `docs/`, `CONTRIBUTING.md`). Ten state and ordering bugs
hide in these subsystems, where the code breaks what its docstrings and the docs say it does:

- the pool's scoring and its events, and when sealed evaluations run. Once a candidate's dev score beats the best,
  each sealed evaluation it needs (its own, and the baseline's while the split has not scored it) is asked for at once
  and runs beside the others when slots are free, and a waiting sealed evaluation gets a slot before its campaign's
  waiting dev evaluations (`piloop eval queue`);
- the recovery of workers and agents after a restart;
- the daemon's passes over the campaigns;
- the delivery of messages to workers. A worker whose harness reads messages only between turns gets each score
  message it has not seen yet in the output of its next `piloop submit`, after that submit's line, once;
- the executor's capacity sample. Where cgroup v2 files exist in `capacity.CGROUP` (`/sys/fs/cgroup`), the CPUs come
  from `cpu.max` (quota ÷ period), the memory from `memory.max − memory.current` and the load from `cpu.pressure`; a
  missing file or `max` keeps the host's number;
- the run controller's scoring and its rules for how a run ended.

Find them and fix them. The final version is scored on held-out tests of these behaviours, with fresh copies of
`tests/`, and scores 0 if a test of `tests/` that passes now fails.

## Measure

`python3 evaluate.py` runs every test of `tests/`, yours included, in about 15 minutes on 2 CPUs, and prints `passed`
(higher is better) and `failed` (but for `SANDBOX_FAILS`, which fail in this sandbox). Test paths as arguments run
fewer. A fix raises `passed` only through a test that shows it, so add one with each. Run tests through it: it clears
the `PILOOP_*` variables, which would point the suite at a live piloop. Do not edit `evaluate.py` or `uv.toml`.

## Where it stands

Every test passes but `SANDBOX_FAILS`. The network reaches the model gateway alone.
