module

public import Silmaril.Semantics.Domain.Function
public import Silmaril.Semantics.Domain.ScottInduction
public import Mathlib.Data.Fin.VecNotation

/-! # Stable functions

Berry's stable functions (§8.2 of the notes, citing Berry [12]). The
continuous function `por : 𝔹⊥ × 𝔹⊥ → 𝔹⊥` is not definable in PCF; Berry's
first step towards a model capturing sequentiality was to refine the model to
*dI-domains* and *stable* functions, so that `por` is no longer an element of
the semantic type `𝔹⊥ → 𝔹⊥ → 𝔹⊥`. A dI-domain is a (Scott) domain in which
binary meets of bounded pairs exist and distribute over binary joins, and
every compact element has finitely many elements below it; we do not develop
dI-domains here, only the notion of stability, which makes sense in any
partial order.

## Stability

A map `f` is *stable* (`Stable`) when it preserves greatest lower bounds of
bounded pairs: whenever `x` and `y` have a common upper bound and `m` is a
greatest lower bound of `{x, y}`, then `f m` is a greatest lower bound of
`{f x, f y}`. We phrase this with Mathlib's `IsGLB` rather than with `⊓`, so
that it applies to any partial order, including function spaces, where meets
need not exist. In a meet-semilattice (for instance a Mathlib
`SemilatticeInf`, or a product of flat domains) it is the familiar equation
`f (x ⊓ y) = f x ⊓ f y` for bounded `x, y` (`stable_iff_map_inf`). Stable
maps are monotone (`Stable.monotone`): take `x ≤ y`.

Stable maps are closed under identity, constants, composition, projections and
pairing (`Stable.id`, `Stable.const`, `Stable.comp`, `Stable.fst`,
`Stable.snd`, `Stable.prodMk`); greatest lower bounds in products are computed
componentwise (`isGLB_pair_prod`, from Mathlib's `isGLB_prod`).

In a flat domain `X⊥ = Part X` two bounded elements are comparable
(`Flat.le_or_le_of_bddAbove`), so *every* monotone map out of a flat domain is
stable (`Stable.of_comparable`, `Flat.stable_of_monotone`); in particular the
flat liftings (`Flat.stable_lift`, `Flat.stable_map`: `succ`, `pred`, `zero?`)
are stable. The conditional `if : 𝔹⊥ × D × D → D` is stable
(`stable_condFun`). Parallel or is not (`Silmaril.Calculi.PCF.not_stable_porFun`).

## The meet relation

The ternary relation `MeetRel d`, "`d 0` and `d 1` are bounded and `d 2` is
their greatest lower bound", is what stable maps preserve (`stable_iff`). On a
flat domain it is Sieber-like (`Flat.meetRel_iff`):

  `(d₀ = ⊥ ∧ d₂ = ⊥) ∨ (d₁ = ⊥ ∧ d₂ = ⊥) ∨ d₀ = d₁ = d₂`,

and it is admissible (`Flat.meetRel_admissible`). Extended to all types as a
logical relation (`Silmaril.Calculi.TypedLambda.MeetLogRel`), it shows that the
first-order denotations of closed PCF terms are stable
(`Silmaril.Calculi.PCF.stable_denoteClosed_bool₂`).

## Berry's stable order

Stable functions are ordered not pointwise but by the *stable order*
(`StableLE`): `f ≤ₛ g` iff `f ≤ g` pointwise and `f x = f y ⊓ g x` whenever
`x ≤ y`. It is a partial order on stable maps (`StableMap`), strictly finer
than the pointwise order (`Flat.not_stableLE_id_const`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

universe u

variable {α β γ : Type*}

section GLB

variable [PartialOrder α]

/-- If `x ≤ y` then `x` is the greatest lower bound of `{x, y}`. -/
theorem isGLB_pair_of_le {x y : α} (h : x ≤ y) : IsGLB {x, y} x := sorry

/-- The pair `{x, y}` is bounded above by `y` when `x ≤ y`. -/
theorem bddAbove_pair_of_le {x y : α} (h : x ≤ y) : BddAbove ({x, y} : Set α) := sorry

end GLB

section Stable

variable [PartialOrder α] [PartialOrder β] [PartialOrder γ]

/-- Berry's stability condition: `f` preserves greatest lower bounds of bounded
pairs. If `x` and `y` have a common upper bound and `m` is a greatest lower
bound of `{x, y}`, then `f m` is a greatest lower bound of `{f x, f y}`. In a
meet-semilattice this reads `f (x ⊓ y) = f x ⊓ f y` (`stable_iff_map_inf`).

This is only the order-theoretic part of Berry's definition: his stable
functions between dI-domains are continuous maps satisfying this condition, and
continuity is kept as a separate hypothesis here. The condition only says
something about bounded pairs that have a greatest lower bound, as all bounded
pairs do in a dI-domain (and in the flat domains and their products used
here); for a bounded pair without one it is vacuous. -/
def Stable (f : α → β) : Prop :=
  ∀ ⦃x y m : α⦄, BddAbove ({x, y} : Set α) → IsGLB {x, y} m → IsGLB {f x, f y} (f m)

/-- Stable maps are monotone. -/
theorem Stable.monotone {f : α → β} (hf : Stable f) : Monotone f := sorry

/-- In meet-semilattices, stability is preservation of meets of bounded
pairs. -/
theorem stable_iff_map_inf {α β : Type*} [SemilatticeInf α] [SemilatticeInf β] {f : α → β} :
    Stable f ↔ ∀ x y, BddAbove ({x, y} : Set α) → f (x ⊓ y) = f x ⊓ f y := sorry

/-- The identity is stable. -/
theorem Stable.id : Stable (id : α → α) := sorry

end Stable

section MeetRel

/-- The meet relation: `d 0` and `d 1` have a common upper bound and `d 2` is
their greatest lower bound. Stable maps are exactly the maps preserving it
(`stable_iff`). -/
def MeetRel [Preorder α] (d : Fin 3 → α) : Prop :=
  BddAbove ({d 0, d 1} : Set α) ∧ IsGLB {d 0, d 1} (d 2)

variable [PartialOrder α] [PartialOrder β]

/-- Stability is preservation of the meet relation. -/
theorem stable_iff {f : α → β} : Stable f ↔ ∀ d : Fin 3 → α, MeetRel d → MeetRel fun i => f (d i) := sorry

end MeetRel

namespace Flat

variable {X : Type u}

end Flat

section Conditional

variable {D : Type*} [OmegaCompletePartialOrder D] [OrderBot D]

end Conditional

section StableOrder

variable [PartialOrder α] [PartialOrder β]

/-- Berry's stable order: `f ≤ₛ g` iff `f ≤ g` pointwise and, whenever
`x ≤ y`, `f x` is the greatest lower bound of `f y` and `g x` (that is,
`f x = f y ⊓ g x`). -/
def StableLE (f g : α → β) : Prop :=
  (∀ x, f x ≤ g x) ∧ ∀ ⦃x y⦄, x ≤ y → IsGLB {f y, g x} (f x)

namespace StableLE

/-- The stable order implies the pointwise order. -/
theorem le {f g : α → β} (h : StableLE f g) (x : α) : f x ≤ g x := sorry

/-- The stable order is reflexive on monotone (in particular stable) maps. -/
theorem refl {f : α → β} (hf : Monotone f) : StableLE f f := sorry

/-- The stable order is transitive. -/
theorem trans {f g h : α → β} (hfg : StableLE f g) (hgh : StableLE g h) : StableLE f h := sorry

/-- The stable order is antisymmetric. -/
theorem antisymm {f g : α → β} (hfg : StableLE f g) (hgf : StableLE g f) : f = g := sorry

end StableLE

/-- Stable maps from `α` to `β`, ordered by Berry's stable order. This is a
minimal carrier for the stable order: a subtype of the function space, with a
coercion to functions (`FunLike`). -/
def StableMap (α β : Type*) [PartialOrder α] [PartialOrder β] : Type _ :=
  {f : α → β // Stable f}

/-- A stable map is a function. -/
instance : FunLike (StableMap α β) α β where
  coe f := f.1
  coe_injective := Subtype.val_injective

/-- A stable map is stable. -/
theorem StableMap.stable (f : StableMap α β) : Stable f := sorry

/-- Berry's stable order makes stable maps a partial order. -/
instance : PartialOrder (StableMap α β) where
  le f g := StableLE f.1 g.1
  le_refl f := StableLE.refl f.2.monotone
  le_trans _ _ _ := StableLE.trans
  le_antisymm _ _ h h' := Subtype.ext (h.antisymm h')

/-- The stable order on `StableMap`. -/
theorem StableMap.le_iff {f g : StableMap α β} : f ≤ g ↔ StableLE f g := sorry

/-- The stable order is strictly finer than the pointwise order: on `Unit⊥`,
the identity lies pointwise below the constant `()`, but not in the stable
order, since `id ⊥ = ⊥` differs from `id () ⊓ const () ⊥ = ()`. -/
theorem Flat.not_stableLE_id_const :
    (∀ x : Part Unit, id x ≤ Part.some ()) ∧ ¬ StableLE (id : Part Unit → Part Unit)
      (fun _ => Part.some ()) := sorry

end StableOrder

end Silmaril.Semantics.Domain
