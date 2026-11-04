module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.DeadCode

/-! # Exercise sheet 4, question 1 (the phase-order problem)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 1.


Answer (notes §8, p. 16): with several optimisations to perform on one
representation, for some programs one order gives better results and for
other programs the opposite order does; moreover choices made within one phase
can enable or disable later ones. A fixed pass order is therefore a compromise.
The simplest instance is that one pass can enable more of itself: DCE is not
idempotent (`Flowgraph.exists_dce_dce_ne`, slides p. 68), so even how often a
pass is repeated matters (`dce_not_idempotent`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q1

/-- Repeating DCE can improve on a single pass. -/
theorem dce_not_idempotent : ∃ G : Flowgraph String (Fin 6), G.dce.dce.code ≠ G.dce.code := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q1
