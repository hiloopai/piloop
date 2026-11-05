module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ControlFlowAnalysis

/-! # Exercise sheet 3, question 1 (higher-order control flow)

Source: `cam-optcomp-2025-exercise-sheet-3` in `references.bib`, question 1.


(a) A function that takes functions as arguments or returns them as results.

(b) At an application `e₁ e₂` the function called is the *value* of `e₁`, which
may come from anywhere in the program; the call graph is no longer syntactic
(cf. `CALLI` in the call graph of notes p. 7).

(c) 0CFA over-approximates, for every program point, the λ-abstractions that
may flow there; at an application `(e₁ʲ e₂)ⁱ` the possible callees are the
λ-abstractions in `αⱼ` (`callees`), a safe over-estimate of the call graph.

(d) Yes: a virtual method call `o.m()` dispatches on the run-time class of `o`,
like applying a function value; class hierarchy analysis (notes §15, p. 28) and
0CFA-like analyses bound the possible targets.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q1

open ControlFlowAnalysis

/-- (c) The possible callees, by label, of an application whose function part has
label `j`. -/
def callees (P : Term) (j : ℕ) : Set ℕ := {p | FlowVal.lam p ∈ P.cfa j}

/-- In the notes' example, the inner application `(id³⁰ id³¹)³²` can only call
`λx.x` (label `22`). -/
theorem callees_notesExample : callees notesExample 30 = {22} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q1
