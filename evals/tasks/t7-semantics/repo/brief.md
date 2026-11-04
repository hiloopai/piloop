# Prove the results of two semantics courses

`Silmaril/` is a Lean 4 library that formalises two undergraduate courses and the calculus under
them: a compiler-optimisation course (flowgraphs, dataflow analyses and the optimisations they
justify, SSA, register allocation, scheduling, abstract interpretation, decompilation, exercises),
and a denotational-semantics course over the PCF calculus (domains, least fixed points, Scott
induction, the PCF denotation, adequacy, full abstraction). Its definitions are complete. No proof of
a result is: every theorem in `spec.json` (`targets`) is stated, with `sorry` as its proof,
including the lemmas the definitions need. A run may add more targets in `spec.private.json`,
under `OptimisingCompilers/Private/`; they count the same. Prove as many of them as you can.

## Rules

- Do not change a definition or a statement: every constant in `spec.json` (`protected`) must
  keep the hash of its elaborated type and, for a definition, of its value. Add any lemmas,
  definitions, tactics and modules you like under `Silmaril/`.
- A proof counts when Lean's kernel accepts it and it uses no axiom beyond `propext`,
  `Classical.choice` and `Quot.sound`: no `sorry`, `native_decide`, `axiom` or `implemented_by`.
  Commands that run code while building (`#eval`, `run_cmd`, `elab`, `initialize`, `IO`) are not
  allowed; `evaluate.py` lists them.
- Do not edit `evaluate.py`, `check/`, `spec.json`, `lakefile.toml`, `lake-manifest.json` or
  `lean-toolchain`. The final version is checked again with fresh copies of them.

## Limits

- The sandbox has 12 GB of memory, as a hard cap: going over it kills the run. A full
  `evaluate.py` peaks near 7 GB, in its audit, so run one at a time.
- The network reaches the model gateway alone: no web, no downloads. Mathlib is already built in
  `.lake/packages`.

## Measure

`python3 evaluate.py` builds the library from scratch, audits the compiled files and prints
`proved`, the share of the targets proved (higher is better), with `proved_count`,
`build_seconds` and, with a private split, `proved_private`. `build/report.txt` lists each unproved target and why. For a fast loop on one file,
`lake env lean Silmaril/Courses/Cambridge/OptimisingCompilers/<File>.lean` checks it alone.

## Where it stands

Every target starts at `sorry`, so `proved` is 0. No lemma is given: the lemmas the original proofs
went through are not in the repo, so plan the intermediate results yourself.
