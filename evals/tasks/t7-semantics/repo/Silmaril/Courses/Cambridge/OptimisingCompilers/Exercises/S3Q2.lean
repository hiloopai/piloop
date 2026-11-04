module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ControlFlowAnalysis

/-! # Exercise sheet 3, question 2 (0CFA with integer flows)

Source: `cam-optcomp-2025-exercise-sheet-3` in `references.bib`, question 2.


(a) A *binding site* introduces a variable: the `v` of `λv.e` and of
`let v = e₁ in e₂` (`Term.lam`, `Term.letE` carry its label). A *use site* is an
occurrence `v` as an expression (`Term.var`).

(b) `exampleB` labels every node; the binder of `x` is `0`.

(c) `alpha0_B`: `α₀ = {2, 3}`. No integer values flow out of the body: 0CFA
does not evaluate `⊕`, so the body's flow set is empty (`body_B`).

(d) The clauses of `Term.Sat` (notes p. 27): a use `vⁱ` bound at `j` gives
`αᵢ ⊇ αⱼ`; `(let vˡ = e₁ʲ in e₂ᵏ)ⁱ` gives `αₗ ⊇ αⱼ` (the variable gets the
bound value) and `αᵢ ⊇ αₖ` (the let yields its body's value).

(e) `alpha1_E`: `α₁ = {λy.y+1}`; `alpha0_E`: `α₀ = {0, 1}`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q2

open ControlFlowAnalysis

/-- (b) `(let f¹⁰ = (λx⁰.(x¹¹ + x¹²)¹³)¹⁴ in ((f¹⁵ 2¹⁶)¹⁷ + (f¹⁸ 3¹⁹)²⁰)²¹)¹`. -/
def exampleB : Term :=
  .letE 1 10 "f" (.lam 14 0 "x" (.binop 13 (.var 11 "x") (.var 12 "x")))
    (.binop 21 (.app 17 (.var 15 "f") (.const 16 2)) (.app 20 (.var 18 "f") (.const 19 3)))

/-- The least solution for `exampleB`. -/
def solB (l : ℕ) : Set FlowVal :=
  if l ∈ [14, 10, 15, 18] then {.lam 14}
  else if l = 16 then {.const 2 16}
  else if l = 19 then {.const 3 19}
  else if l ∈ [0, 11, 12] then {.const 2 16, .const 3 19}
  else ∅

/-- (c) `α₀ = {2, 3}`. -/
theorem alpha0_B : exampleB.cfa 0 = {.const 2 16, .const 3 19} := sorry

/-- (c) Nothing flows out of the body `x + x`. -/
theorem body_B : exampleB.cfa 13 = ∅ := sorry

/-- (e) `(let f²⁰ = (λx²¹.(x¹ 0²²)²³)²⁴ in
(let g²⁵ = (λy⁰.(y²⁶ + 1²⁷)²⁸)²⁹ in ((f³⁰ g³¹)³² + (g³³ 1³⁴)³⁵)³⁶)³⁷)³⁸`. -/
def exampleE : Term :=
  .letE 38 20 "f" (.lam 24 21 "x" (.app 23 (.var 1 "x") (.const 22 0)))
    (.letE 37 25 "g" (.lam 29 0 "y" (.binop 28 (.var 26 "y") (.const 27 1)))
      (.binop 36 (.app 32 (.var 30 "f") (.var 31 "g")) (.app 35 (.var 33 "g") (.const 34 1))))

/-- The least solution for `exampleE`. -/
def solE (l : ℕ) : Set FlowVal :=
  if l ∈ [24, 20, 30] then {.lam 24}
  else if l ∈ [29, 25, 31, 33, 21, 1] then {.lam 29}
  else if l = 22 then {.const 0 22}
  else if l = 27 then {.const 1 27}
  else if l = 34 then {.const 1 34}
  else if l ∈ [0, 26] then {.const 0 22, .const 1 34}
  else ∅

/-- (e) `α₁ = {λy.y+1}`. -/
theorem alpha1_E : exampleE.cfa 1 = {.lam 29} := sorry

/-- (e) `α₀ = {0, 1}`. -/
theorem alpha0_E : exampleE.cfa 0 = {.const 0 22, .const 1 34} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q2
