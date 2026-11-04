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
    BddAbove ({x, y} : Set D) ↔ ∃ z, x ≤ z ∧ y ≤ z := by
  simp [BddAbove, upperBounds_insert, upperBounds_singleton, Set.Nonempty]

/-- `z` is a least upper bound of `{x, y}` iff it bounds both and lies below
every common upper bound. -/
theorem isLUB_pair_iff {D : Type*} [Preorder D] {x y z : D} :
    IsLUB ({x, y} : Set D) z ↔ (x ≤ z ∧ y ≤ z) ∧ ∀ w, x ≤ w → y ≤ w → z ≤ w := by
  simp [IsLUB, IsLeast, upperBounds_insert, upperBounds_singleton, lowerBounds]

/-- `⊥` is compatible with everything. -/
theorem bddAbove_pair_bot_left {D : Type*} [Preorder D] [OrderBot D] (x : D) :
    BddAbove ({⊥, x} : Set D) :=
  bddAbove_pair_iff_exists.2 ⟨x, bot_le, le_rfl⟩

/-- Everything is compatible with `⊥`. -/
theorem bddAbove_pair_bot_right {D : Type*} [Preorder D] [OrderBot D] (x : D) :
    BddAbove ({x, ⊥} : Set D) :=
  bddAbove_pair_iff_exists.2 ⟨x, le_rfl, bot_le⟩

/-- Two defined elements of a flat domain are compatible only when equal. -/
theorem Flat.bddAbove_some_some_iff {X : Type*} {a b : X} :
    BddAbove ({Part.some a, Part.some b} : Set (Part X)) ↔ a = b := by
  rw [bddAbove_pair_iff_exists]
  constructor
  · rintro ⟨z, ha, hb⟩
    exact Part.some_injective ((Flat.eq_of_some_le ha).symm.trans (Flat.eq_of_some_le hb))
  · rintro rfl
    exact ⟨_, le_rfl, le_rfl⟩

/-- Flat domains are bounded complete. -/
theorem Flat.boundedComplete (X : Type*) : BoundedComplete (Part X) := by
  intro x y hb
  obtain ⟨z, hxz, hyz⟩ := bddAbove_pair_iff_exists.1 hb
  by_cases hx : x = Part.none
  · subst hx
    exact ⟨y, isLUB_pair_iff.2 ⟨⟨bot_le, le_rfl⟩, fun _ _ h => h⟩⟩
  · have hxz' : x = z := (Flat.le_iff_eq_none_or_eq.1 hxz).resolve_left hx
    subst hxz'
    exact ⟨x, isLUB_pair_iff.2 ⟨⟨le_rfl, hyz⟩, fun _ h _ => h⟩⟩

section Pi

/-- Pi types: a pair is bounded iff it is bounded in every coordinate. -/
theorem bddAbove_pair_pi_iff {ι : Type*} {P : ι → Type*} [∀ i, Preorder (P i)]
    {x y : ∀ i, P i} :
    BddAbove ({x, y} : Set (∀ i, P i)) ↔ ∀ i, BddAbove ({x i, y i} : Set (P i)) := by
  rw [bddAbove_pi]
  simp only [Set.image_insert_eq, Set.image_singleton, Function.eval]

/-- Doubly indexed pi types (such as environments): two families whose coordinates
are pairwise compatible have a common upper bound. -/
theorem exists_le_le_of_forall_bddAbove₂ {ι : Type*} {κ : ι → Type*}
    {P : ∀ i, κ i → Type*} [∀ i j, Preorder (P i j)] {x y : ∀ i j, P i j}
    (h : ∀ i j, BddAbove ({x i j, y i j} : Set (P i j))) : ∃ z, x ≤ z ∧ y ≤ z :=
  bddAbove_pair_iff_exists.1 (bddAbove_pair_pi_iff.2 fun i => bddAbove_pair_pi_iff.2 (h i))

end Pi

namespace ContinuousHom

variable {D E : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- Two continuous functions which are pointwise bounded into a bounded
complete domain have a least upper bound, computed pointwise. -/
theorem exists_isLUB_pair (hE : BoundedComplete E) {f g : D →𝒄 E}
    (h : ∀ x, BddAbove ({f x, g x} : Set E)) :
    ∃ w : D →𝒄 E, IsLUB ({f, g} : Set (D →𝒄 E)) w := by
  choose w hw using fun x => hE _ _ (h x)
  have hw' : ∀ x, (f x ≤ w x ∧ g x ≤ w x) ∧ ∀ k, f x ≤ k → g x ≤ k → w x ≤ k :=
    fun x => isLUB_pair_iff.1 (hw x)
  have hmono : Monotone w := fun x x' hxx' =>
    (hw' x).2 _ ((f.monotone hxx').trans (hw' x').1.1) ((g.monotone hxx').trans (hw' x').1.2)
  have hcont : ωScottContinuous w := by
    refine ωScottContinuous_of_le hmono fun c => (hw' _).2 _ ?_ ?_
    · rw [f.continuous c]
      exact ωSup_le _ _ fun n =>
        (hw' (c n)).1.1.trans (le_ωSup (c.map ⟨w, hmono⟩) n)
    · rw [g.continuous c]
      exact ωSup_le _ _ fun n =>
        (hw' (c n)).1.2.trans (le_ωSup (c.map ⟨w, hmono⟩) n)
  refine ⟨ContinuousHom.ofFun w hcont, isLUB_pair_iff.2 ⟨⟨?_, ?_⟩, ?_⟩⟩
  · exact fun x => (hw' x).1.1
  · exact fun x => (hw' x).1.2
  · exact fun k hf hg x => (hw' x).2 _ (hf x) (hg x)

/-- Into a bounded complete domain, two continuous functions are compatible
iff they are pointwise compatible. -/
theorem bddAbove_pair_iff (hE : BoundedComplete E) {f g : D →𝒄 E} :
    BddAbove ({f, g} : Set (D →𝒄 E)) ↔ ∀ x, BddAbove ({f x, g x} : Set E) := by
  constructor
  · intro hb x
    obtain ⟨k, hf, hg⟩ := bddAbove_pair_iff_exists.1 hb
    exact bddAbove_pair_iff_exists.2 ⟨k x, hf x, hg x⟩
  · intro h
    obtain ⟨w, hw⟩ := exists_isLUB_pair hE h
    exact ⟨w, hw.1⟩

/-- Continuous functions into a bounded complete domain form a bounded complete
domain. -/
theorem boundedComplete (hE : BoundedComplete E) : BoundedComplete (D →𝒄 E) :=
  fun _ _ hb => exists_isLUB_pair hE ((bddAbove_pair_iff hE).1 hb)

end ContinuousHom

end Silmaril.Semantics.Domain
