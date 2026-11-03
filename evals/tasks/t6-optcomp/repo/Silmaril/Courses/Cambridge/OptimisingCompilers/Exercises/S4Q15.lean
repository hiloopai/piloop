module

/-! # Exercise sheet 4, question 15 (pruning points-to with liveness)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 15.


Answer: yes. A pair `(x, a)` at program point `n` is only ever consulted when `x`
is later dereferenced or copied, i.e. when `x` is live at `n`; if `x` is dead at
`n` its current value can never be read, so all pairs `(x, _)` can be dropped at
`n` without affecting any client of the analysis. Pairs `(a, b)` for heap or
address-taken `a` must be kept, since `*y` may read them through an alias.

Discussion question; status: answered in prose. A formal statement needs a
flow-sensitive points-to analysis over the course flowgraph IR (future work).
-/
