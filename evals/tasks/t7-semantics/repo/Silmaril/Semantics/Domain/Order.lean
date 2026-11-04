module

public import Mathlib.Order.Bounds.Basic
public import Mathlib.Order.FixedPoints

/-! # Least pre-fixed points

Definitions 1–5 of the denotational semantics notes (§2.1–2.2) are Mathlib's
`PartialOrder`, `Monotone`, `IsLeast` and `Function.fixedPoints`. This module
adds pre-fixed points and Proposition 1: for a monotone function, a least
pre-fixed point is a least fixed point. On a complete lattice, least pre-fixed
points are Mathlib's `OrderHom.lfp` (`IsLeastPrefixedPoint.eq_lfp`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

variable {D : Type*} [PartialOrder D]

/-- Definition 5: `d` is a pre-fixed point of `f` when `f d ⊑ d`. -/
def IsPrefixedPoint (f : D → D) (d : D) : Prop := f d ≤ d

/-- Definition 5: `d` is the least pre-fixed point of `f`, written `fix(f)` in
the notes. -/
def IsLeastPrefixedPoint (f : D → D) (d : D) : Prop :=
  IsLeast {x | IsPrefixedPoint f x} d

namespace IsLeastPrefixedPoint

variable {f : D → D} {d : D}

/-- Property (lfp-fix). -/
theorem prefixed (h : IsLeastPrefixedPoint f d) : f d ≤ d := h.1

/-- Property (lfp-least). -/
theorem le (h : IsLeastPrefixedPoint f d) {x : D} (hx : f x ≤ x) : d ≤ x := h.2 hx

/-- The two properties (lfp-fix) and (lfp-least) characterize `fix(f)`. -/
theorem of_le (hfix : f d ≤ d) (hleast : ∀ x, f x ≤ x → d ≤ x) :
    IsLeastPrefixedPoint f d := ⟨hfix, fun _ hx => hleast _ hx⟩

/-- Least pre-fixed points are unique. -/
theorem unique {e : D} (hd : IsLeastPrefixedPoint f d) (he : IsLeastPrefixedPoint f e) :
    d = e := IsLeast.unique hd he

/-- Proposition 1: a least pre-fixed point of a monotone function is fixed. -/
theorem isFixedPt (hf : Monotone f) (h : IsLeastPrefixedPoint f d) : f d = d :=
  le_antisymm h.prefixed (h.le (hf h.prefixed))

/-- Proposition 1: a least pre-fixed point of a monotone function is the least
fixed point. -/
theorem isLeast_fixedPoints (hf : Monotone f) (h : IsLeastPrefixedPoint f d) :
    IsLeast (Function.fixedPoints f) d :=
  ⟨h.isFixedPt hf, fun _ hx => h.le (le_of_eq hx)⟩

end IsLeastPrefixedPoint

section CompleteLattice

variable {α : Type*} [CompleteLattice α]

/-- Mathlib's complete-lattice least fixed point is a least pre-fixed point. -/
theorem _root_.OrderHom.isLeastPrefixedPoint_lfp (f : α →o α) :
    IsLeastPrefixedPoint f f.lfp :=
  f.isLeast_lfp_le

/-- On a complete lattice, a least pre-fixed point is Mathlib's `OrderHom.lfp`. -/
theorem IsLeastPrefixedPoint.eq_lfp {f : α →o α} {d : α} (h : IsLeastPrefixedPoint f d) :
    d = f.lfp :=
  h.unique f.isLeastPrefixedPoint_lfp

end CompleteLattice

end Silmaril.Semantics.Domain
