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

end SieberRel

end Silmaril.Semantics.Domain
