module

public import Silmaril.Semantics.Domain.Continuity

/-! # Discrete and flat domains

Definitions 11–12 of the notes (§3.1). The flat domain `X⊥` is Mathlib's
`Part X`: its order `x ⊑ y ↔ ∀ a ∈ x, a ∈ y` is exactly the flat order
(`Part.le_iff_eq_none_or_eq`), and `⊥` is `Part.none`. Flat domains have no
infinite strictly ascending chains, so monotone maps out of them (and out of
finite products of them) are continuous.

The flat lifting `Flat.lift f` is `Part.bind` and `Flat.map f` is `Part.map`
(`lift_apply`, `map_apply_eq_partMap`); within a single universe they are
Mathlib's `ContinuousHom.bind` and `ContinuousHom.map`
(`lift_eq_continuousHom_bind`, `map_eq_continuousHom_map`). We keep our own
definitions because Mathlib's require source and target in a single universe,
whereas ours are universe-polymorphic (`X : Type u`, `Y : Type v`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

universe u v

/-- Definition 11: a set with the discrete order. -/
def Discrete (X : Type u) : Type u := X

namespace Discrete

variable {X : Type u}

/-- Embed an element into the discrete order. -/
def of (x : X) : Discrete X := x

/-- Recover an element of the discrete order. -/
def get (x : Discrete X) : X := x

instance : PartialOrder (Discrete X) where
  le x y := x = y
  le_refl _ := rfl
  le_trans _ _ _ := Eq.trans
  le_antisymm _ _ h _ := h

theorem le_iff {x y : Discrete X} : x ≤ y ↔ x = y := sorry

/-- Chains in a discrete order are constant. -/
theorem chain_eq (c : Chain (Discrete X)) (n : ℕ) : c n = c 0 := sorry

/-- Definition 11: a discrete order is a cpo. -/
instance : OmegaCompletePartialOrder (Discrete X) where
  ωSup c := c 0
  le_ωSup c n := (chain_eq c n).le
  ωSup_le _ _ h := h 0

end Discrete

namespace Flat

variable {X : Type u} {Y : Type v}

/-- Definition 12: the flat order, `x ⊑ y` iff `x = ⊥` or `x = y`. -/
theorem le_iff_eq_none_or_eq {x y : Part X} : x ≤ y ↔ x = Part.none ∨ x = y := sorry

/-- `Part.none` is the least element (`⊥`) of a flat domain. -/
@[simp] theorem none_le (x : Part X) : Part.none ≤ x := sorry

/-- The flat Booleans have exactly three elements. -/
theorem bool_cases (b : Part Bool) :
    b = Part.none ∨ b = Part.some true ∨ b = Part.some false := sorry

/-- A defined element of a flat domain is maximal. -/
theorem eq_of_some_le {a : X} {y : Part X} (h : Part.some a ≤ y) : y = Part.some a := sorry

/-- The elements above a defined element of a flat domain: only itself. -/
theorem some_le_iff {a : X} {y : Part X} : Part.some a ≤ y ↔ y = Part.some a := sorry

/-- The two defined flat Booleans are distinct. -/
theorem some_true_ne_some_false : (Part.some true : Part Bool) ≠ Part.some false := sorry

theorem lt_iff {x y : Part X} : x < y ↔ x = Part.none ∧ y.Dom := sorry

private theorem acc_of_dom {y : Part X} (hy : y.Dom) : Acc (· > ·) y := sorry

/-- Flat domains have no infinite strictly ascending chains. -/
instance : WellFoundedGT (Part X) :=
  ⟨fun _ => Acc.intro _ fun _ hz => acc_of_dom (lt_iff.1 hz).2⟩

/-- Example 6: a partial function `X ⇀ Y` is the same as a monotone map from
the discrete order on `X` to `Y⊥`. -/
def partialFunEquiv : (X → Part Y) ≃ (Discrete X →o Part Y) where
  toFun f := ⟨fun x => f x.get, fun _ _ h => (Discrete.le_iff.1 h) ▸ le_rfl⟩
  invFun g x := g (Discrete.of x)
  left_inv _ := rfl
  right_inv _ := rfl

/-- Proposition 7: the flat lifting of a partial function, strict and continuous. -/
noncomputable def lift (f : X → Part Y) : Part X →𝒄 Part Y :=
  toContinuousHom (fun x => x.bind f) <|
    ωScottContinuous_of_monotone fun x y h => by
      rcases le_iff_eq_none_or_eq.1 h with rfl | rfl
      · simp only [Part.bind_none]
        exact bot_le
      · exact le_rfl

@[simp] theorem lift_some (f : X → Part Y) (a : X) : lift f (Part.some a) = f a := sorry

@[simp] theorem lift_none (f : X → Part Y) : lift f Part.none = Part.none := sorry

/-- The flat lifting of a total function. -/
noncomputable def map (f : X → Y) : Part X →𝒄 Part Y := lift fun a => Part.some (f a)

@[simp] theorem map_some (f : X → Y) (a : X) : map f (Part.some a) = Part.some (f a) := sorry

@[simp] theorem map_none (f : X → Y) : map f Part.none = Part.none := sorry

/-- The flat lifting is `Part.bind`. -/
theorem lift_apply (f : X → Part Y) (x : Part X) : lift f x = x.bind f := sorry

section SameUniverse

variable {X' Y' : Type u}

/-- Within one universe, the flat lifting is Mathlib's `ContinuousHom.bind`. -/
theorem lift_eq_continuousHom_bind (f : X' → Part Y') :
    lift f = ContinuousHom.bind ContinuousHom.id (ContinuousHom.const f) := sorry

/-- Within one universe, the flat lifting of a total function is Mathlib's
`ContinuousHom.map`. -/
theorem map_eq_continuousHom_map (f : X' → Y') :
    map f = ContinuousHom.map f ContinuousHom.id := sorry

end SameUniverse

end Flat

end Silmaril.Semantics.Domain
