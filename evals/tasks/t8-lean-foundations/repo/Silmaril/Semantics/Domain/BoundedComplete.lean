module

public import Silmaril.Semantics.Domain.Function
public import Silmaril.Semantics.Domain.Flat

/-! # Bounded completeness

A preorder is *bounded complete* (for pairs) when every pair of elements with a
common upper bound has a least upper bound. Flat domains are bounded complete
(`Flat.boundedComplete`), and bounded completeness passes to continuous
function spaces (`ContinuousHom.boundedComplete`), where the least upper bound
of a bounded pair is computed pointwise (`ContinuousHom.exists_isLUB_pair`).
In particular two continuous functions are compatible exactly when they are
pointwise compatible (`ContinuousHom.bddAbove_pair_iff`). For products (pi
types) a pair is bounded exactly when it is bounded in every coordinate
(`bddAbove_pair_pi_iff`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

/-- Every pair with an upper bound has a least upper bound. -/
def BoundedComplete (D : Type*) [Preorder D] : Prop :=
  ∀ x y : D, BddAbove ({x, y} : Set D) → ∃ z, IsLUB ({x, y} : Set D) z

/-- A pair is bounded above iff it has a common upper bound. -/
theorem bddAbove_pair_iff_exists {D : Type*} [Preorder D] {x y : D} :
    BddAbove ({x, y} : Set D) ↔ ∃ z, x ≤ z ∧ y ≤ z := sorry

/-- `z` is a least upper bound of `{x, y}` iff it bounds both and lies below
every common upper bound. -/
theorem isLUB_pair_iff {D : Type*} [Preorder D] {x y z : D} :
    IsLUB ({x, y} : Set D) z ↔ (x ≤ z ∧ y ≤ z) ∧ ∀ w, x ≤ w → y ≤ w → z ≤ w := sorry

/-- `⊥` is compatible with everything. -/
theorem bddAbove_pair_bot_left {D : Type*} [Preorder D] [OrderBot D] (x : D) :
    BddAbove ({⊥, x} : Set D) := sorry

/-- Everything is compatible with `⊥`. -/
theorem bddAbove_pair_bot_right {D : Type*} [Preorder D] [OrderBot D] (x : D) :
    BddAbove ({x, ⊥} : Set D) := sorry

/-- Two defined elements of a flat domain are compatible only when equal. -/
theorem Flat.bddAbove_some_some_iff {X : Type*} {a b : X} :
    BddAbove ({Part.some a, Part.some b} : Set (Part X)) ↔ a = b := sorry

/-- Flat domains are bounded complete. -/
theorem Flat.boundedComplete (X : Type*) : BoundedComplete (Part X) := sorry

section Pi

/-- Pi types: a pair is bounded iff it is bounded in every coordinate. -/
theorem bddAbove_pair_pi_iff {ι : Type*} {P : ι → Type*} [∀ i, Preorder (P i)]
    {x y : ∀ i, P i} :
    BddAbove ({x, y} : Set (∀ i, P i)) ↔ ∀ i, BddAbove ({x i, y i} : Set (P i)) := sorry

/-- Doubly indexed pi types (such as environments): two families whose coordinates
are pairwise compatible have a common upper bound. -/
theorem exists_le_le_of_forall_bddAbove₂ {ι : Type*} {κ : ι → Type*}
    {P : ∀ i, κ i → Type*} [∀ i j, Preorder (P i j)] {x y : ∀ i j, P i j}
    (h : ∀ i j, BddAbove ({x i j, y i j} : Set (P i j))) : ∃ z, x ≤ z ∧ y ≤ z := sorry

end Pi

namespace ContinuousHom

variable {D E : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- Two continuous functions which are pointwise bounded into a bounded
complete domain have a least upper bound, computed pointwise. -/
theorem exists_isLUB_pair (hE : BoundedComplete E) {f g : D →𝒄 E}
    (h : ∀ x, BddAbove ({f x, g x} : Set E)) :
    ∃ w : D →𝒄 E, IsLUB ({f, g} : Set (D →𝒄 E)) w := sorry

/-- Into a bounded complete domain, two continuous functions are compatible
iff they are pointwise compatible. -/
theorem bddAbove_pair_iff (hE : BoundedComplete E) {f g : D →𝒄 E} :
    BddAbove ({f, g} : Set (D →𝒄 E)) ↔ ∀ x, BddAbove ({f x, g x} : Set E) := sorry

/-- Continuous functions into a bounded complete domain form a bounded complete
domain. -/
theorem boundedComplete (hE : BoundedComplete E) : BoundedComplete (D →𝒄 E) := sorry

end ContinuousHom

end Silmaril.Semantics.Domain
