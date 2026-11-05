module

public import Silmaril.Calculi.Imp.BigStep
public import Silmaril.Semantics.Domain.ScottInduction
import Mathlib.Tactic.Ring

/-! # The factorial loop

The running example of the notes (§1.2, Example 13 and Example 16): with two
locations `X` and `Y`, the program

  `while X > 0 do (Y := X * Y; X := X - 1)`     (3)

denotes the least fixed point of

  `F(w)[X ↦ x, Y ↦ y] = [X ↦ x, Y ↦ y]` if `x ≤ 0`,
  `F(w)[X ↦ x, Y ↦ y] = w[X ↦ x - 1, Y ↦ x ⋅ y]` if `x > 0`.

We compute its Kleene approximations `wₙ = Fⁿ(⊥)` and their union `w∞` as
tabulated on p. 10, and prove the partial correctness assertion of Example 16
by Scott induction.

Two small corrections to the notes:

* The table for `wₙ` on p. 10 holds for `n ≥ 1` only: `w₀ = ⊥` is undefined
  everywhere, while the table would make it defined when `x < 0`.
* In the stability step of Example 16 the notes write
  `F(w)[X ↦ x, Y ↦ y](Y) = x ⋅ y` when `x > 0`. The value is really
  `w[X ↦ x - 1, Y ↦ x ⋅ y](Y)`, which is non-negative because `w ∈ S` and
  `x ⋅ y ≥ 0`.
-/

@[expose] public section

namespace Silmaril.Calculi.Imp.Factorial


open OmegaCompletePartialOrder Silmaril.Semantics.Domain

/-- §1.2: the locations `X` and `Y` of the running example. -/
inductive Loc where
  | X
  | Y
  deriving DecidableEq

open Loc

/-- The state `[X ↦ x, Y ↦ y]`. -/
def st (x y : ℤ) : State Loc
  | X => x
  | Y => y

@[simp] theorem st_X (x y : ℤ) : st x y X = x := sorry

@[simp] theorem st_Y (x y : ℤ) : st x y Y = y := sorry

/-- The loop guard `X > 0`. -/
def guard : Bexp Loc := .lt (.num 0) (.loc X)

/-- The loop body `Y := X * Y; X := X - 1`. -/
def body : Prog Loc :=
  .seq (.assign Y (.mul (.loc X) (.loc Y))) (.assign X (.sub (.loc X) (.num 1)))

/-- §1.2, program (3): `while X > 0 do (Y := X * Y; X := X - 1)`. -/
def factorialProg : Prog Loc := .while guard body

/-- §1.2: the functional `F = F_{⟦X > 0⟧, ⟦Y := X * Y; X := X - 1⟧}`. -/
noncomputable def factF :
    (State Loc → Part (State Loc)) →𝒄 (State Loc → Part (State Loc)) :=
  F B⟦guard⟧ C⟦body⟧

/-- §1.2: the Kleene approximations `wₙ = Fⁿ(⊥)`. -/
noncomputable def approx (n : ℕ) : State Loc → Part (State Loc) := factF^[n] ⊥

/-- The closed form of `w∞` (p. 10), as a total function on states. -/
def wInf (s : State Loc) : State Loc :=
  if s X < 0 then s else st 0 ((s X).toNat.factorial * s Y)

section Example16

/-- Example 16: for fixed `x` and `y`, the set of `w` with
`(w[X ↦ x, Y ↦ y] ⇓) ⇒ w[X ↦ x, Y ↦ y](Y) ≥ 0`. -/
def PostNonneg (x y : ℤ) : Set (State Loc → Part (State Loc)) :=
  {w | ∀ t ∈ w (st x y), 0 ≤ t Y}

/-- Example 16: the set `S = ⋂_{x, y ≥ 0} PostNonneg x y`. -/
def nonnegInvariant : Set (State Loc → Part (State Loc)) :=
  ⋂ (x : ℤ) (y : ℤ) (_ : 0 ≤ y), PostNonneg x y

end Example16

end Silmaril.Calculi.Imp.Factorial
