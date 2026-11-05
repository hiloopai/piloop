module

/-! # Exercise sheet 4, question 16 (liveness versus points-to)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 16.


Answer (sketch). Yes. LVA with pointers needs points-to information to resolve
ambiguous references: `t = *p` references every variable `p` may point to (notes
p. 9). Pruning points-to sets with liveness (question 15) needs LVA. Running
either first with the other's safe default (all address-taken variables, or no
pruning) loses precision, and the precise answer is a simultaneous fixed point
of both analyses.

Status: **open** (discussion; a formal statement needs the combined analysis
over a flowgraph IR with pointers).
-/
