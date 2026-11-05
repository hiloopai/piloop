module

public import Silmaril.Calculi.Imp.Factorial


/-! # §1 Introduction (pp. 4–11)

Index of §1 of the notes against `Silmaril.Calculi.Imp`. The chapter has no numbered
items, so the wrappers are named after the equations they state. Each one
restates the notes' equation and is proved by the general development.

Programs denote partial functions `State → Part State` throughout, as the
notes do from p. 9 on; on p. 6–7 they are total. Sequencing is then Kleisli
composition (`Part.bind`) rather than plain composition. Expressions always
terminate, so the guard `b` of `F_{b,c}` is total, where Exercise 3 allows it
to be partial.

Correction to the notes: the table for `wₙ` on p. 10 holds for `n ≥ 1` only,
since `w₀ = ⊥` is undefined everywhere while the table makes it defined at
states with `x < 0`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.Introduction

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain Silmaril.Calculi.Imp Silmaril.Calculi.Imp.Factorial

variable {L : Type*}

/-! ## §1.1 A basic example (pp. 5–7) -/

/-- §1.1 (p. 6): `A⟦n⟧ = λs ∈ State. n`. -/
theorem aexp_num (n : ℤ) : A⟦(.num n : Aexp L)⟧ = fun _ => n := sorry

/-- §1.1 (p. 6): `A⟦L⟧ = λs ∈ State. s(L)`. -/
theorem aexp_loc (l : L) : A⟦(.loc l : Aexp L)⟧ = fun s => s l := sorry

/-- §1.1 (p. 6): `A⟦A₁ + A₂⟧ = λs ∈ State. A⟦A₁⟧(s) + A⟦A₂⟧(s)`. -/
theorem aexp_add (a₁ a₂ : Aexp L) : A⟦.add a₁ a₂⟧ = fun s => A⟦a₁⟧ s + A⟦a₂⟧ s := sorry

/-- §1.1 (p. 6): `B⟦true⟧ = λs ∈ State. true`. -/
theorem bexp_true : B⟦(.tt : Bexp L)⟧ = fun _ => true := sorry

/-- §1.1 (p. 6): `B⟦false⟧ = λs ∈ State. false`. -/
theorem bexp_false : B⟦(.ff : Bexp L)⟧ = fun _ => false := sorry

/-- §1.1 (p. 6): `B⟦A₁ = A₂⟧ = λs ∈ State. eq(A⟦A₁⟧(s), A⟦A₂⟧(s))`. -/
theorem bexp_eq (a₁ a₂ : Aexp L) :
    B⟦.eq a₁ a₂⟧ = fun s => decide (A⟦a₁⟧ s = A⟦a₂⟧ s) := sorry

variable [DecidableEq L]

/-- §1.1 (p. 7): `C⟦skip⟧ = λs ∈ State. s`. -/
theorem prog_skip : C⟦(.skip : Prog L)⟧ = fun s => Part.some s := sorry

/-- §1.1 (p. 7): `C⟦if B then C else C'⟧ = λs. if(B⟦B⟧(s), C⟦C⟧(s), C⟦C'⟧(s))`. -/
theorem prog_ite (b : Bexp L) (c c' : Prog L) :
    C⟦.ite b c c'⟧ = fun s => if B⟦b⟧ s then C⟦c⟧ s else C⟦c'⟧ s := sorry

/-- §1.1 (p. 7): `C⟦L := A⟧ = λs ∈ State. s[L ↦ A⟦A⟧(s)]`. -/
theorem prog_assign (l : L) (a : Aexp L) :
    C⟦.assign l a⟧ = fun s => Part.some (Function.update s l (A⟦a⟧ s)) := sorry

/-- §1.1 (p. 7): `C⟦C; C'⟧ = C⟦C'⟧ ∘ C⟦C⟧`, as composition of partial functions. -/
theorem prog_seq (c c' : Prog L) : C⟦.seq c c'⟧ = fun s => (C⟦c⟧ s).bind C⟦c'⟧ := sorry

/-! ## §1.2 A semantics for loops (pp. 7–10) -/

omit [DecidableEq L] in
/-- §1.2 (p. 7): `F_{b,c}(w) = λs ∈ State. if(b(s), w ∘ c(s), s)`. -/
theorem F_apply (b : State L → Bool) (c : State L → Part (State L))
    (w : State L → Part (State L)) (s : State L) :
    F b c w s = if b s then (c s).bind w else Part.some s := sorry

/-- §1.2 (p. 7): `⟦while B do C⟧` solves the fixed point equation
`⟦while B do C⟧ = F_{⟦B⟧,⟦C⟧}(⟦while B do C⟧)`. -/
theorem while_eq (b : Bexp L) (c : Prog L) : C⟦.while b c⟧ = F B⟦b⟧ C⟦c⟧ C⟦.while b c⟧ := sorry

/-- §1.2 (p. 7): `⟦while B do C⟧ = ⟦if B then (C; while B do C) else skip⟧`. -/
theorem while_unfold (b : Bexp L) (c : Prog L) :
    C⟦.while b c⟧ = C⟦.ite b (.seq c (.while b c)) .skip⟧ := sorry

/-- §1.2 (p. 9): program (3), `while X > 0 do (Y := X * Y; X := X - 1)`, denotes
the least fixed point of the functional `F` given by
`F(w)[X ↦ x, Y ↦ y] = [X ↦ x, Y ↦ y]` if `x ≤ 0` and
`w[X ↦ x - 1, Y ↦ x ⋅ y]` if `x > 0`. -/
theorem program3_F (w : State Loc → Part (State Loc)) (x y : ℤ) :
    factF w (st x y) = if x ≤ 0 then Part.some (st x y) else w (st (x - 1) (x * y)) := sorry

/-- §1.2 (p. 9): `w₀ = ⊥` and `wₙ₊₁ = F(wₙ)`. -/
theorem program3_approx (n : ℕ) : approx 0 = ⊥ ∧ approx (n + 1) = factF (approx n) := sorry

/-- §1.2 (p. 10): the table for `wₙ`, corrected to `n ≥ 1`:
`wₙ[X ↦ x, Y ↦ y]` is `[X ↦ x, Y ↦ y]` if `x < 0`, `[X ↦ 0, Y ↦ x! ⋅ y]` if
`0 ≤ x < n`, and undefined if `x ≥ n`. -/
theorem program3_approx_st {n : ℕ} (hn : 0 < n) (x y : ℤ) :
    approx n (st x y) =
      if x < 0 then Part.some (st x y)
      else if x < n then Part.some (st 0 (x.toNat.factorial * y))
      else Part.none := sorry

/-- §1.2 (p. 10): `w∞ = ⨆ᵢ wᵢ` maps `[X ↦ x, Y ↦ y]` to itself if `x < 0` and to
`[X ↦ 0, Y ↦ x! ⋅ y]` if `x ≥ 0`. -/
theorem program3_wInf (x y : ℤ) : fix factF (st x y) = Part.some (wInf (st x y)) := sorry

/-- §1.2 (p. 10): `w∞` is the least fixed point of `F`, and it is the denotation
of program (3). -/
theorem program3_isLeast :
    IsLeast (Function.fixedPoints factF) (fix factF) ∧
      C⟦factorialProg⟧ = fun s => Part.some (wInf s) := sorry

/-- §1.2 (p. 10): "`w∞` is indeed the function from states to states that we get
from the operational semantics". This holds for every program: the big-step
semantics of Part IB agrees with the denotation. -/
theorem operational_eq_denote {c : Prog L} {s s' : State L} : Exec c s s' ↔ s' ∈ C⟦c⟧ s := sorry

/-- §1.2 (p. 10): the operational semantics of program (3) is `w∞`. -/
theorem program3_operational {s s' : State Loc} : Exec factorialProg s s' ↔ s' = wInf s := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.Introduction
