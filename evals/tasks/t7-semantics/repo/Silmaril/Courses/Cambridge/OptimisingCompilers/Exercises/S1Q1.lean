module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.DeadCode
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Runs
public import Mathlib.Tactic.Ring

/-! # Exercise sheet 1, question 1 (LVA and dead-code elimination)

Source: `cam-optcomp-2025-exercise-sheet-1` in `references.bib`, question 1.


The function is the flowgraph `f`: node 0 is `ENTRY`, nodes 1–8 are the eight
statements, and node 9 is `return y` (an `EXIT` returning `y`).

* (a) `lva_f`: the live-in sets.
* (b) `dce_f`: one pass removes `z = z+y` (node 4); `dce_dce_f`: a second pass,
  on the new liveness, also removes `z = y+y` (node 1).
* (c) `f_simplified`: `f` behaves as `return x+2`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q1

/-- The function `f` of question 1. -/
def f : Flowgraph String (Fin 10) where
  entry := 0
  code := ![.entry 1,
    .alu .add "z" (.var "y") (.var "y") 2,
    .alu .add "y" (.var "x") (.imm 1) 3,
    .alu .sub "x" (.var "x") (.var "y") 4,
    .alu .add "z" (.var "z") (.var "y") 5,
    .alu .add "x" (.var "x") (.imm 1) 6,
    .alu .add "y" (.var "y") (.var "x") 7,
    .alu .add "z" (.var "x") (.imm 1) 8,
    .alu .add "y" (.var "y") (.var "z") 9,
    .exit ["y"]]

/-- (a) The live-in sets. -/
theorem lva_f :
    f.lva = ![{"x", "y"}, {"x", "y"}, {"x", "z"}, {"x", "y", "z"}, {"x", "y", "z"},
      {"x", "y"}, {"x", "y"}, {"x", "y"}, {"y", "z"}, {"y"}] := sorry

/-- (b) One DCE pass removes exactly `z = z+y`. -/
theorem dce_f : ∀ n, f.dce.code n = if n = 4 then .nop 5 else f.code n := sorry

/-- (b) A second pass also removes `z = y+y`, whose result was only used by the
removed instruction. -/
theorem dce_dce_f :
    ∀ n, f.dce.dce.code n = if n = 4 then .nop 5 else if n = 1 then .nop 2 else f.code n := sorry

/-- (c) The simplified function: `return x+2`. -/
def fSimplified : Flowgraph String (Fin 3) where
  entry := 0
  code := ![.entry 1, .alu .add "y" (.var "x") (.imm 2) 2, .exit ["y"]]

/-- (c) `f` and `return x+2` have the same outcomes from every initial state. -/
theorem f_simplified (env : Env String) (mem : Mem) :
    f.outcomes ⟨0, env, mem⟩ = fSimplified.outcomes ⟨0, env, mem⟩ ∧
      f.traces ⟨0, env, mem⟩ = fSimplified.traces ⟨0, env, mem⟩ := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q1
