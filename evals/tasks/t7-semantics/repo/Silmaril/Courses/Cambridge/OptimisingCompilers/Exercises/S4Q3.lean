module

/-! # Exercise sheet 4, question 3 (SSA in compilation and decompilation)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 3.


Answer. In compilation (notes §7, pp. 15–16), SSA gives every definition its
own name. This splits live ranges (fewer clashes, so better register
allocation), makes use–def chains explicit, and simplifies analyses such as
constant propagation and CSE. In decompilation (Mycroft 1999; slides p. 129),
SSA separates unrelated uses of one machine register, so each SSA variable can
get its own source variable and type by type inference; φ-functions mark the
joins that structured control flow must reproduce.

The SSA translation is `Silmaril.Courses.Cambridge.OptimisingCompilers.Assign.toSSA`.
Discussion question; status: answered in prose, no formal content.
-/
