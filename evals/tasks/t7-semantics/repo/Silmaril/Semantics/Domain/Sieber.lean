module

public import Silmaril.Semantics.Domain.ScottInduction

/-! # Sieber's ternary relation

On a pointed set, Sieber's relation holds of a triple `(d₀, d₁, d₂)` when

  `d₀ = ⊥ ∨ d₁ = ⊥ ∨ d₀ = d₁ = d₂`.

It is the base case of the logical relation behind the undefinability of
parallel or (Theorem 40 of the notes; see `Silmaril.Calculi.TypedLambda.SieberLogRel` and
`Silmaril.Calculi.PCF.por_undefinable`). Its point is that a *sequential* function,
which must inspect one of its arguments first, preserves it, whereas parallel
or does not.

Triples are functions `Fin 3 → D`, so that on an ω-cpo the relation is a
subset of the product domain and `Admissible` applies directly. The relation
is admissible (Propositions 18–20), preserved by constant triples and by
strict maps.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D E : Type*}

/-- Sieber's ternary relation on a pointed set: `d₀ = ⊥`, or `d₁ = ⊥`, or
`d₀ = d₁ = d₂`. -/
def SieberRel [Bot D] (d : Fin 3 → D) : Prop :=
  d 0 = ⊥ ∨ d 1 = ⊥ ∨ (d 0 = d 1 ∧ d 1 = d 2)

namespace SieberRel

/-- Constant triples are related. -/
theorem const [Bot D] (x : D) : SieberRel fun _ : Fin 3 => x :=
  Or.inr (Or.inr ⟨rfl, rfl⟩)

/-- Strict functions preserve the relation. -/
theorem map [Bot D] [Bot E] {g : D → E} (hg : g ⊥ = ⊥) {d : Fin 3 → D}
    (h : SieberRel d) : SieberRel fun i => g (d i) := by
  rcases h with h | h | ⟨h₁, h₂⟩
  · exact Or.inl (by simp only [h, hg])
  · exact Or.inr (Or.inl (by simp only [h, hg]))
  · exact Or.inr (Or.inr ⟨congrArg g h₁, congrArg g h₂⟩)

/-- Three related elements, the first two of which are not `⊥`, are equal. -/
theorem eq_zero [Bot D] {d : Fin 3 → D} (h : SieberRel d) (h₀ : d 0 ≠ ⊥)
    (h₁ : d 1 ≠ ⊥) : ∀ i : Fin 3, d i = d 0 := by
  obtain ⟨e₁, e₂⟩ := (h.resolve_left h₀).resolve_left h₁
  intro i
  match i with
  | 0 => rfl
  | 1 => exact e₁.symm
  | 2 => exact (e₁.trans e₂).symm

/-- The relation is admissible: it contains `(⊥, ⊥, ⊥)` and is a finite
union of intersections of equalities between continuous functions, hence
chain-closed (Propositions 18–20). -/
theorem admissible [OmegaCompletePartialOrder D] [OrderBot D] :
    Admissible {d : Fin 3 → D | SieberRel d} := by
  have eq : ∀ i j : Fin 3, ChainClosed {d : Fin 3 → D | d i = d j} := fun i j =>
    ChainClosed.eq_of_continuous (ωScottContinuous.apply i) (ωScottContinuous.apply j)
  have bot : ∀ i : Fin 3, ChainClosed {d : Fin 3 → D | d i = ⊥} := fun i =>
    ChainClosed.eq_of_continuous (ωScottContinuous.apply i) (ωScottContinuous_const ⊥)
  exact ⟨Or.inl rfl, (bot 0).union ((bot 1).union ((eq 0 1).inter (eq 1 2)))⟩

end SieberRel

end Silmaril.Semantics.Domain
