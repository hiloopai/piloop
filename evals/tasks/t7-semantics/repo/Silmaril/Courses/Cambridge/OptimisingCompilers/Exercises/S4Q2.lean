module

/-! # Exercise sheet 4, question 2 (conflicting phases)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 2.


Answer (notes §5 and §8, pp. 13–16): common-subexpression elimination keeps a
value live in a new temporary `t` from its computation to its reuse. This
lengthens live ranges and adds clashes, so a register allocator run afterwards
may have to spill `t` (or another variable) to memory; if the expression is
cheap, recomputing it would have been faster than the `LD`/`ST` of the spill.
Conversely, allocating registers first constrains CSE, which can no longer
introduce temporaries freely. Instruction scheduling and register allocation
conflict in the same way (notes §20, p. 37): scheduling lengthens live ranges,
and allocation adds false dependencies that restrict scheduling.

Discussion question; status: answered in prose, no formal content.
-/
