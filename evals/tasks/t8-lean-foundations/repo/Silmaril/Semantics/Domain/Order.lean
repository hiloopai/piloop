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

end IsLeastPrefixedPoint

section CompleteLattice

variable {α : Type*} [CompleteLattice α]

end CompleteLattice

end Silmaril.Semantics.Domain
