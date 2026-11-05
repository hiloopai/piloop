module

public import Silmaril.Semantics.Domain.Examples
public import Silmaril.Semantics.Domain.Function

/-! # Continuous predicates on the vertical naturals

Exercise 6 of the notes: the function domain `Ω → {⊤}⊥` is in bijection with
`Ω = ℕ∞`. A continuous `f` is determined by where it becomes defined. That set
is upward closed and, by continuity, contains no infinite element unless it
contains a finite one. So it is either empty or `[n, ω]` for a finite `n`.
The bijection sends `f` to that threshold, with `ω` for the empty set; it
reverses the order.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

namespace OmegaThreshold

open Classical in
/-- The predicate that becomes defined from the finite threshold `n` on
(nowhere, when `n = ω`). -/
noncomputable def predicate (n : ℕ∞) (x : ℕ∞) : Part Unit :=
  if n ≠ ⊤ ∧ n ≤ x then Part.some () else Part.none

theorem predicate_dom {n x : ℕ∞} : (predicate n x).Dom ↔ n ≠ ⊤ ∧ n ≤ x := sorry

/-- A finite element below a lub of naturals is below some element of the chain. -/
theorem exists_le_of_le_ωSup {m : ℕ} {c : Chain ℕ∞} (h : (m : ℕ∞) ≤ ωSup c) :
    ∃ k, (m : ℕ∞) ≤ c k := sorry

theorem predicate_continuous (n : ℕ∞) : ωScottContinuous (predicate n) := sorry

/-- The continuous predicate with threshold `n`. -/
noncomputable def toFun (n : ℕ∞) : ℕ∞ →𝒄 Part Unit :=
  ContinuousHom.ofFun (predicate n) (predicate_continuous n)

/-- The threshold of a continuous predicate: the least point where it is
defined. -/
noncomputable def threshold (f : ℕ∞ →𝒄 Part Unit) : ℕ∞ := sInf {x | (f x).Dom}

theorem dom_mono (f : ℕ∞ →𝒄 Part Unit) {x y : ℕ∞} (hxy : x ≤ y) (hx : (f x).Dom) :
    (f y).Dom := sorry

/-- A continuous predicate defined at `ω` is defined at some finite point. -/
theorem exists_nat_dom (f : ℕ∞ →𝒄 Part Unit) (h : (f ⊤).Dom) : ∃ n : ℕ, (f n).Dom := sorry

theorem part_unit_eq {p q : Part Unit} (h : p.Dom ↔ q.Dom) : p = q := sorry

theorem threshold_toFun (n : ℕ∞) : threshold (toFun n) = n := sorry

theorem toFun_threshold (f : ℕ∞ →𝒄 Part Unit) : toFun (threshold f) = f := sorry

/-- Exercise 6: `Ω → {⊤}⊥` is in bijection with `Ω`. -/
noncomputable def equiv : (ℕ∞ →𝒄 Part Unit) ≃ ℕ∞ where
  toFun := threshold
  invFun := toFun
  left_inv := toFun_threshold
  right_inv := threshold_toFun

end OmegaThreshold

end Silmaril.Semantics.Domain
