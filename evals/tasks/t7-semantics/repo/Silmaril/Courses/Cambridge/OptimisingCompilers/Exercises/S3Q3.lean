module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ControlFlowAnalysis

/-! # Exercise sheet 3, question 3 (0CFA of function flows)

Source: `cam-optcomp-2025-exercise-sheet-3` in `references.bib`, question 3.


The analysis `Term.cfa` tracks constants too; the function values are its
`FlowVal.lam` elements, and restricting to them gives the requested analysis.

(a) With the labelling of `example3`, `alphaX`: `x` may be bound to either
argument, `{λy.y∗3, λz.z+1}`, so the application `x 0` may call either; `f` is
only ever `λx.x 0` (`alphaF`).

(b) The clauses of `Term.Sat` (notes p. 27): `(λxʲ.eᵏ)ⁱ` gives
`αᵢ ⊇ {(λxʲ.eᵏ)ⁱ}` (an abstraction evaluates to itself), and `(e₁ʲ e₂ᵏ)ⁱ` gives,
for every `(λx_q.e_r)ₚ` that may flow to `αⱼ`, `α_q ⊇ αₖ` (the argument flows to
the parameter) and `αᵢ ⊇ α_r` (the body's result flows out of the call).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q3

open ControlFlowAnalysis

/-- `(let f¹ = (λx².(x³ 0⁴)⁵)⁶ in
((f⁷ (λy⁸.(y⁹ ∗ 3¹⁰)¹¹)¹²)¹³ + (f¹⁴ (λz¹⁵.(z¹⁶ + 1¹⁷)¹⁸)¹⁹)²⁰)²¹)²²`. -/
def example3 : Term :=
  .letE 22 1 "f" (.lam 6 2 "x" (.app 5 (.var 3 "x") (.const 4 0)))
    (.binop 21
      (.app 13 (.var 7 "f") (.lam 12 8 "y" (.binop 11 (.var 9 "y") (.const 10 3))))
      (.app 20 (.var 14 "f") (.lam 19 15 "z" (.binop 18 (.var 16 "z") (.const 17 1)))))

/-- The function values flowing to a point. -/
def funFlows (P : Term) (l : ℕ) : Set ℕ := {p | FlowVal.lam p ∈ P.cfa l}

/-- The least solution for `example3`. -/
def sol3 (l : ℕ) : Set FlowVal :=
  if l ∈ [6, 1, 7, 14] then {.lam 6}
  else if l = 12 then {.lam 12}
  else if l = 19 then {.lam 19}
  else if l ∈ [2, 3] then {.lam 12, .lam 19}
  else if l = 4 then {.const 0 4}
  else if l = 10 then {.const 3 10}
  else if l = 17 then {.const 1 17}
  else if l ∈ [8, 9, 15, 16] then {.const 0 4}
  else ∅

/-- (a) `x` may be either argument. -/
theorem alphaX : funFlows example3 2 = {12, 19} := sorry

/-- (a) `f` is only ever `λx.x 0`. -/
theorem alphaF : funFlows example3 1 = {6} := sorry

/-- (a) No function flows to `y` or `z`, or out of the calls. -/
theorem alpha_rest : funFlows example3 8 = ∅ ∧ funFlows example3 15 = ∅ ∧
    funFlows example3 13 = ∅ ∧ funFlows example3 20 = ∅ := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q3
