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

theorem le_iff {x y : Discrete X} : x ≤ y ↔ x = y := Iff.rfl

/-- Chains in a discrete order are constant. -/
theorem chain_eq (c : Chain (Discrete X)) (n : ℕ) : c n = c 0 :=
  (c.monotone (Nat.zero_le n)).symm

/-- Definition 11: a discrete order is a cpo. -/
instance : OmegaCompletePartialOrder (Discrete X) where
  ωSup c := c 0
  le_ωSup c n := (chain_eq c n).le
  ωSup_le _ _ h := h 0

end Discrete

namespace Flat

variable {X : Type u} {Y : Type v}

/-- Definition 12: the flat order, `x ⊑ y` iff `x = ⊥` or `x = y`. -/
theorem le_iff_eq_none_or_eq {x y : Part X} : x ≤ y ↔ x = Part.none ∨ x = y := by
  constructor
  · intro h
    by_cases hx : x.Dom
    · exact Or.inr (Part.ext fun a =>
        ⟨fun ha => h a ha, fun ha => by
          obtain rfl := Part.mem_unique ha (h _ (Part.get_mem hx))
          exact Part.get_mem hx⟩)
    · exact Or.inl (Part.eq_none_iff'.2 hx)
  · rintro (rfl | rfl)
    · exact bot_le
    · exact le_rfl

/-- `Part.none` is the least element (`⊥`) of a flat domain. -/
@[simp] theorem none_le (x : Part X) : Part.none ≤ x := bot_le

/-- The flat Booleans have exactly three elements. -/
theorem bool_cases (b : Part Bool) :
    b = Part.none ∨ b = Part.some true ∨ b = Part.some false := by
  by_cases h : b.Dom
  · rw [Part.dom_iff_mem] at h
    obtain ⟨v, hv⟩ := h
    rw [Part.eq_some_iff.2 hv]
    cases v <;> simp
  · exact Or.inl (Part.eq_none_iff'.2 h)

/-- A defined element of a flat domain is maximal. -/
theorem eq_of_some_le {a : X} {y : Part X} (h : Part.some a ≤ y) : y = Part.some a := by
  rcases le_iff_eq_none_or_eq.1 h with h | h
  · exact absurd h (Part.some_ne_none a)
  · exact h.symm

/-- The elements above a defined element of a flat domain: only itself. -/
theorem some_le_iff {a : X} {y : Part X} : Part.some a ≤ y ↔ y = Part.some a :=
  ⟨eq_of_some_le, fun h => by rw [h]⟩

/-- The two defined flat Booleans are distinct. -/
theorem some_true_ne_some_false : (Part.some true : Part Bool) ≠ Part.some false := by
  simp

theorem lt_iff {x y : Part X} : x < y ↔ x = Part.none ∧ y.Dom := by
  constructor
  · intro h
    rcases le_iff_eq_none_or_eq.1 h.le with hx | hx
    · refine ⟨hx, ?_⟩
      by_contra hy
      exact h.ne (hx.trans (Part.eq_none_iff'.2 hy).symm)
    · exact absurd hx h.ne
  · rintro ⟨rfl, hy⟩
    exact lt_of_le_of_ne bot_le fun h => (Part.eq_none_iff'.1 h.symm) hy

private theorem acc_of_dom {y : Part X} (hy : y.Dom) : Acc (· > ·) y :=
  Acc.intro _ fun _ hz => absurd hy (Part.eq_none_iff'.1 (lt_iff.1 hz).1)

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

@[simp] theorem lift_some (f : X → Part Y) (a : X) : lift f (Part.some a) = f a :=
  Part.bind_some a f

@[simp] theorem lift_none (f : X → Part Y) : lift f Part.none = Part.none :=
  Part.bind_none f

/-- Proposition 7: the flat lifting is strict. -/
theorem lift_strict (f : X → Part Y) : Strict (lift f) := lift_none f

/-- The flat lifting of a total function. -/
noncomputable def map (f : X → Y) : Part X →𝒄 Part Y := lift fun a => Part.some (f a)

@[simp] theorem map_some (f : X → Y) (a : X) : map f (Part.some a) = Part.some (f a) :=
  lift_some _ a

@[simp] theorem map_none (f : X → Y) : map f Part.none = Part.none := lift_none _

/-- The flat lifting is `Part.bind`. -/
theorem lift_apply (f : X → Part Y) (x : Part X) : lift f x = x.bind f := rfl

/-- The flat lifting of a total function is `Part.map`. -/
theorem map_apply_eq_partMap (f : X → Y) (x : Part X) : map f x = Part.map f x :=
  Part.bind_some_eq_map f x

section SameUniverse

variable {X' Y' : Type u}

/-- Within one universe, the flat lifting is Mathlib's `ContinuousHom.bind`. -/
theorem lift_eq_continuousHom_bind (f : X' → Part Y') :
    lift f = ContinuousHom.bind ContinuousHom.id (ContinuousHom.const f) := by
  ext x
  rfl

/-- Within one universe, the flat lifting of a total function is Mathlib's
`ContinuousHom.map`. -/
theorem map_eq_continuousHom_map (f : X' → Y') :
    map f = ContinuousHom.map f ContinuousHom.id := by
  ext x
  rw [map_apply_eq_partMap, ContinuousHom.map_apply]
  rfl

end SameUniverse

/-- Exercise 5 (corrected): a monotone map between flat domains is strict or
constant. The notes' alternative (b) asks only for constancy on `X`, which
does not imply monotonicity (take `f ⊥ = 0` and `f x = 1`); constancy on all
of `X⊥` is the right condition. -/
theorem strict_or_const_of_monotone {f : Part X → Part Y} (hf : Monotone f) :
    Strict f ∨ ∀ d, f d = f ⊥ := by
  by_cases h : (f ⊥).Dom
  · refine Or.inr fun d => ?_
    have hsome : f ⊥ = Part.some ((f ⊥).get h) := Part.get_eq_iff_eq_some.1 rfl
    rw [hsome]
    exact eq_of_some_le (hsome ▸ hf bot_le)
  · exact Or.inl (Part.eq_none_iff'.2 h)

/-- Exercise 5 (corrected): a map between flat domains is continuous iff it is
strict or constant. -/
theorem continuous_iff_strict_or_const {f : Part X → Part Y} :
    ωScottContinuous f ↔ Strict f ∨ ∀ d, f d = f ⊥ := by
  refine ⟨fun h => strict_or_const_of_monotone h.monotone, fun h => ?_⟩
  refine ωScottContinuous_of_monotone ?_
  rcases h with h | h
  · intro x y hxy
    rcases le_iff_eq_none_or_eq.1 hxy with rfl | rfl
    · change f ⊥ ≤ _
      rw [show f ⊥ = ⊥ from h]
      exact bot_le
    · exact le_rfl
  · intro x y _
    rw [h x, h y]

end Flat

end Silmaril.Semantics.Domain
