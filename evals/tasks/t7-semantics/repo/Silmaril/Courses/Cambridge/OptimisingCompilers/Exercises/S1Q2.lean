module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Liveness
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Runs
public import Mathlib.Data.Fin.VecNotation

/-! # Exercise sheet 1, question 2 (syntactic and semantic liveness)

Source: `cam-optcomp-2025-exercise-sheet-1` in `references.bib`, question 2.


The flowgraph `h`: 0 `ENTRY`; 1 `y = x`; 2 `z = 0`; 3 the loop test
`CMPGT x, #0`; 4 the test `if (0)`, compiled as `CMPNE #0, #0`; 5 `z = z+y`;
6 `x = x-1`; 7 `return z`.

* (a) `lva_h`: the live-in sets.
* (b) The definitions are `Flowgraph.SynLive` and `Flowgraph.SemLive` (notes
  p. 8). `y` is syntactically live in the loop, along the path through the dead
  branch `z = z+y`, but not semantically live, since that branch never runs
  (`y_synLive_not_semLive`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q2

/-- The function `h` of question 2. -/
def h : Flowgraph String (Fin 8) where
  entry := 0
  code := ![.entry 1,
    .alu2 .mov "y" (.var "x") 2,
    .alu2 .mov "z" (.imm 0) 3,
    .cmp .gt (.var "x") (.imm 0) 4 7,
    .cmp .ne (.imm 0) (.imm 0) 5 6,
    .alu .add "z" (.var "z") (.var "y") 6,
    .alu .sub "x" (.var "x") (.imm 1) 3,
    .exit ["z"]]

/-- (a) The live-in sets. -/
theorem lva_h :
    h.lva = ![{"x"}, {"x"}, {"x", "y"}, {"x", "y", "z"}, {"x", "y", "z"}, {"x", "y", "z"},
      {"x", "y", "z"}, {"z"}] := sorry

/-- `h` with the never-taken test `if (0)` replaced by a jump past it. -/
def hJump : Flowgraph String (Fin 8) where
  entry := 0
  code := fun n => if n = 4 then .nop 6 else h.code n

/-- (b) `y` is syntactically but not semantically live at the loop test. -/
theorem y_synLive_not_semLive : h.SynLive 3 "y" ∧ ¬ h.SemLive 3 "y" := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q2
