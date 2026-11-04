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
theorem isGLB_pair_of_le {x y : α} (h : x ≤ y) : IsGLB {x, y} x := by
  refine ⟨fun z hz => ?_, fun l hl => hl (Set.mem_insert _ _)⟩
  rcases hz with rfl | rfl
  exacts [le_rfl, h]

/-- The greatest lower bound of `{x, y}` when `x ≤ y` is `x`. -/
theorem isGLB_pair_eq_of_le {x y m : α} (hm : IsGLB {x, y} m) (h : x ≤ y) : m = x :=
  hm.unique (isGLB_pair_of_le h)

/-- The pair `{x, y}` is bounded above by `y` when `x ≤ y`. -/
theorem bddAbove_pair_of_le {x y : α} (h : x ≤ y) : BddAbove ({x, y} : Set α) :=
  ⟨y, fun z hz => by rcases hz with rfl | rfl; exacts [h, le_rfl]⟩

/-- Greatest lower bounds of pairs in a product are computed componentwise. -/
theorem isGLB_pair_prod [PartialOrder β] {p q m : α × β} :
    IsGLB {p, q} m ↔ IsGLB {p.1, q.1} m.1 ∧ IsGLB {p.2, q.2} m.2 := by
  rw [isGLB_prod, Set.image_pair, Set.image_pair]

/-- Pairs in a product are bounded iff their components are. -/
theorem bddAbove_pair_prod [PartialOrder β] {p q : α × β} :
    BddAbove ({p, q} : Set (α × β)) ↔
      BddAbove ({p.1, q.1} : Set α) ∧ BddAbove ({p.2, q.2} : Set β) := by
  rw [bddAbove_prod, Set.image_pair, Set.image_pair]

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
theorem Stable.monotone {f : α → β} (hf : Stable f) : Monotone f := fun _ _ h =>
  (hf (bddAbove_pair_of_le h) (isGLB_pair_of_le h)).1 (Set.mem_insert_of_mem _ rfl)

/-- In meet-semilattices, stability is preservation of meets of bounded
pairs. -/
theorem stable_iff_map_inf {α β : Type*} [SemilatticeInf α] [SemilatticeInf β] {f : α → β} :
    Stable f ↔ ∀ x y, BddAbove ({x, y} : Set α) → f (x ⊓ y) = f x ⊓ f y := by
  refine ⟨fun hf x y hb => (hf hb isGLB_pair).unique isGLB_pair, fun hf x y m hb hm => ?_⟩
  rw [hm.unique isGLB_pair, hf x y hb]
  exact isGLB_pair

/-- The identity is stable. -/
theorem Stable.id : Stable (id : α → α) := fun _ _ _ _ hm => hm

/-- Constant maps are stable. -/
theorem Stable.const (c : β) : Stable fun _ : α => c := fun _ _ _ _ _ => by
  rw [Set.pair_eq_singleton]
  exact isGLB_singleton

/-- Monotone maps send bounded pairs to bounded pairs. -/
theorem bddAbove_pair_image {f : α → β} (hf : Monotone f) {x y : α}
    (hb : BddAbove ({x, y} : Set α)) : BddAbove ({f x, f y} : Set β) := by
  rw [← Set.image_pair]
  exact hf.map_bddAbove hb

/-- Stable maps compose. -/
theorem Stable.comp {g : β → γ} {f : α → β} (hg : Stable g) (hf : Stable f) :
    Stable (g ∘ f) := fun _ _ _ hb hm =>
  hg (bddAbove_pair_image hf.monotone hb) (hf hb hm)

/-- The first projection is stable. -/
theorem Stable.fst : Stable (Prod.fst : α × β → α) := fun _ _ _ _ hm =>
  (isGLB_pair_prod.1 hm).1

/-- The second projection is stable. -/
theorem Stable.snd : Stable (Prod.snd : α × β → β) := fun _ _ _ _ hm =>
  (isGLB_pair_prod.1 hm).2

/-- The pairing of stable maps is stable. -/
theorem Stable.prodMk {f : α → β} {g : α → γ} (hf : Stable f) (hg : Stable g) :
    Stable fun x => (f x, g x) := fun _ _ _ hb hm =>
  isGLB_pair_prod.2 ⟨hf hb hm, hg hb hm⟩

/-- If bounded pairs in `α` are comparable, every monotone map out of `α` is
stable. -/
theorem Stable.of_comparable
    (hα : ∀ x y : α, BddAbove ({x, y} : Set α) → x ≤ y ∨ y ≤ x) {f : α → β}
    (hf : Monotone f) : Stable f := by
  intro x y m hb hm
  rcases hα x y hb with h | h
  · rw [isGLB_pair_eq_of_le hm h]
    exact isGLB_pair_of_le (hf h)
  · rw [Set.pair_comm] at hm ⊢
    rw [isGLB_pair_eq_of_le hm h]
    exact isGLB_pair_of_le (hf h)

end Stable

section MeetRel

/-- The meet relation: `d 0` and `d 1` have a common upper bound and `d 2` is
their greatest lower bound. Stable maps are exactly the maps preserving it
(`stable_iff`). -/
def MeetRel [Preorder α] (d : Fin 3 → α) : Prop :=
  BddAbove ({d 0, d 1} : Set α) ∧ IsGLB {d 0, d 1} (d 2)

variable [PartialOrder α] [PartialOrder β]

/-- Constant triples are related. -/
theorem MeetRel.const (x : α) : MeetRel fun _ : Fin 3 => x :=
  ⟨bddAbove_pair_of_le le_rfl, isGLB_pair_of_le le_rfl⟩

/-- A triple `(x, y, x)` with `x ≤ y` is related. -/
theorem MeetRel.of_le {d : Fin 3 → α} (h : d 0 ≤ d 1) (h₂ : d 2 = d 0) : MeetRel d :=
  ⟨bddAbove_pair_of_le h, by rw [h₂]; exact isGLB_pair_of_le h⟩

/-- A triple `(⊥, y, ⊥)` is related. -/
theorem MeetRel.of_bot_left [OrderBot α] {d : Fin 3 → α} (h₀ : d 0 = ⊥) (h₂ : d 2 = ⊥) :
    MeetRel d :=
  .of_le (h₀ ▸ bot_le) (h₂.trans h₀.symm)

/-- A triple `(x, ⊥, ⊥)` is related. -/
theorem MeetRel.of_bot_right [OrderBot α] {d : Fin 3 → α} (h₁ : d 1 = ⊥) (h₂ : d 2 = ⊥) :
    MeetRel d := by
  refine ⟨?_, ?_⟩
  · rw [Set.pair_comm]
    exact bddAbove_pair_of_le (h₁ ▸ bot_le)
  · rw [Set.pair_comm, h₂, ← h₁]
    exact isGLB_pair_of_le (h₁ ▸ bot_le)

/-- A stable map preserves the meet relation. -/
theorem Stable.meetRel {f : α → β} (hf : Stable f) {d : Fin 3 → α} (hd : MeetRel d) :
    MeetRel fun i => f (d i) :=
  ⟨bddAbove_pair_image hf.monotone hd.1, hf hd.1 hd.2⟩

/-- Stability is preservation of the meet relation. -/
theorem stable_iff {f : α → β} : Stable f ↔ ∀ d : Fin 3 → α, MeetRel d → MeetRel fun i => f (d i) :=
  ⟨fun hf _ hd => hf.meetRel hd, fun hf x y m hb hm => (hf ![x, y, m] ⟨hb, hm⟩).2⟩

end MeetRel

namespace Flat

variable {X : Type u}

/-- In a flat domain, two elements with a common upper bound are comparable. -/
theorem le_or_le_of_bddAbove {x y : Part X} (h : BddAbove ({x, y} : Set (Part X))) :
    x ≤ y ∨ y ≤ x := by
  obtain ⟨z, hz⟩ := h
  have hx : x ≤ z := hz (Set.mem_insert _ _)
  have hy : y ≤ z := hz (Set.mem_insert_of_mem _ rfl)
  rcases le_iff_eq_none_or_eq.1 hx with rfl | rfl
  · exact Or.inl bot_le
  · exact Or.inr hy

/-- Every monotone map out of a flat domain is stable. -/
theorem stable_of_monotone {f : Part X → β} [PartialOrder β] (hf : Monotone f) : Stable f :=
  Stable.of_comparable (fun _ _ => le_or_le_of_bddAbove) hf

/-- The flat lifting of a partial function is stable. -/
theorem stable_lift {Y : Type*} (f : X → Part Y) : Stable (lift f) :=
  stable_of_monotone (lift f).monotone

/-- The flat lifting of a total function is stable. -/
theorem stable_map {Y : Type*} (f : X → Y) : Stable (map f) :=
  stable_of_monotone (map f).monotone

/-- On a flat domain the meet relation is Sieber-like: `(⊥, y, ⊥)`,
`(x, ⊥, ⊥)` or `(x, x, x)`. -/
theorem meetRel_iff {d : Fin 3 → Part X} :
    MeetRel d ↔ (d 0 = ⊥ ∧ d 2 = ⊥) ∨ (d 1 = ⊥ ∧ d 2 = ⊥) ∨ (d 0 = d 1 ∧ d 1 = d 2) := by
  constructor
  · rintro ⟨hb, hm⟩
    rcases le_or_le_of_bddAbove hb with h | h
    · have h₂ := isGLB_pair_eq_of_le hm h
      rcases le_iff_eq_none_or_eq.1 h with h₀ | h₀
      · exact Or.inl ⟨h₀, h₂.trans h₀⟩
      · exact Or.inr (Or.inr ⟨h₀, h₀ ▸ h₂.symm⟩)
    · rw [Set.pair_comm] at hm
      have h₂ := isGLB_pair_eq_of_le hm h
      rcases le_iff_eq_none_or_eq.1 h with h₁ | h₁
      · exact Or.inr (Or.inl ⟨h₁, h₂.trans h₁⟩)
      · exact Or.inr (Or.inr ⟨h₁.symm, h₂.symm⟩)
  · rintro (⟨h₀, h₂⟩ | ⟨h₁, h₂⟩ | ⟨h₀, h₂⟩)
    · exact MeetRel.of_bot_left h₀ h₂
    · exact MeetRel.of_bot_right h₁ h₂
    · exact MeetRel.of_le h₀.le (h₂.symm.trans h₀.symm)

/-- On a flat domain the meet relation is admissible: it contains `(⊥, ⊥, ⊥)`
and it is chain-closed, being (by `meetRel_iff`) a finite union of
intersections of equalizers of continuous maps, which are chain-closed
(`ChainClosed.eq_of_continuous`). -/
theorem meetRel_admissible : Admissible {d : Fin 3 → Part X | MeetRel d} := by
  have eq : ∀ i j : Fin 3, ChainClosed {d : Fin 3 → Part X | d i = d j} := fun i j =>
    ChainClosed.eq_of_continuous (ωScottContinuous.apply i) (ωScottContinuous.apply j)
  have bot : ∀ i : Fin 3, ChainClosed {d : Fin 3 → Part X | d i = ⊥} := fun i =>
    ChainClosed.eq_of_continuous (ωScottContinuous.apply i) (ωScottContinuous_const ⊥)
  have hS : {d : Fin 3 → Part X | MeetRel d} =
      ({d | d 0 = ⊥} ∩ {d | d 2 = ⊥}) ∪ (({d | d 1 = ⊥} ∩ {d | d 2 = ⊥}) ∪
        ({d | d 0 = d 1} ∩ {d | d 1 = d 2})) :=
    Set.ext fun _ => meetRel_iff
  rw [hS]
  exact ⟨Or.inl ⟨rfl, rfl⟩, ((bot 0).inter (bot 2)).union
    (((bot 1).inter (bot 2)).union ((eq 0 1).inter (eq 1 2)))⟩

end Flat

section Conditional

variable {D : Type*} [OmegaCompletePartialOrder D] [OrderBot D]

/-- The conditional sends a pair of triples, with comparable conditions
`p.1 ≤ q.1`, and a greatest lower bound `m` of them to a greatest lower bound. -/
private theorem isGLB_condFun_of_le {p q m : Part Bool × D × D} (h : p.1 ≤ q.1)
    (hm₁ : IsGLB {p.1, q.1} m.1) (hx : IsGLB {p.2.1, q.2.1} m.2.1)
    (hy : IsGLB {p.2.2, q.2.2} m.2.2) :
    IsGLB {condFun p.1 p.2.1 p.2.2, condFun q.1 q.2.1 q.2.2} (condFun m.1 m.2.1 m.2.2) := by
  rw [isGLB_pair_eq_of_le hm₁ h]
  rcases Flat.le_iff_eq_none_or_eq.1 h with h₀ | h₀
  · simp only [h₀, condFun_none]
    exact isGLB_pair_of_le bot_le
  · rw [← h₀]
    rcases Flat.bool_cases p.1 with h' | h' | h'
    · simp only [h', condFun_none, Set.pair_eq_singleton]
      exact isGLB_singleton
    · simpa only [h', condFun_true] using hx
    · simpa only [h', condFun_false] using hy

/-- The conditional `if : 𝔹⊥ × D × D → D` is stable. -/
theorem stable_condFun : Stable fun p : Part Bool × D × D => condFun p.1 p.2.1 p.2.2 := by
  intro p q m hb hm
  have hb₁ := (bddAbove_pair_prod.1 hb).1
  obtain ⟨hm₁, hm₂⟩ := isGLB_pair_prod.1 hm
  obtain ⟨hx, hy⟩ := isGLB_pair_prod.1 hm₂
  rcases Flat.le_or_le_of_bddAbove hb₁ with h | h
  · exact isGLB_condFun_of_le h hm₁ hx hy
  · rw [Set.pair_comm] at hm₁ hx hy ⊢
    exact isGLB_condFun_of_le h hm₁ hx hy

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
theorem le {f g : α → β} (h : StableLE f g) (x : α) : f x ≤ g x := h.1 x

/-- The stable order is reflexive on monotone (in particular stable) maps. -/
theorem refl {f : α → β} (hf : Monotone f) : StableLE f f := by
  refine ⟨fun _ => le_rfl, fun x y h => ?_⟩
  rw [Set.pair_comm]
  exact isGLB_pair_of_le (hf h)

/-- The stable order is transitive. -/
theorem trans {f g h : α → β} (hfg : StableLE f g) (hgh : StableLE g h) : StableLE f h := by
  refine ⟨fun x => (hfg.le x).trans (hgh.le x), fun x y hxy => ⟨?_, fun l hl => ?_⟩⟩
  · rintro z (rfl | rfl)
    · exact (hfg.2 hxy).1 (Set.mem_insert _ _)
    · exact (hfg.le x).trans (hgh.le x)
  · have hly : l ≤ f y := hl (Set.mem_insert _ _)
    have hlh : l ≤ h x := hl (Set.mem_insert_of_mem _ rfl)
    have hlg : l ≤ g x := (hgh.2 hxy).2 fun z hz => by
      rcases hz with rfl | rfl
      exacts [hly.trans (hfg.le y), hlh]
    exact (hfg.2 hxy).2 fun z hz => by
      rcases hz with rfl | rfl
      exacts [hly, hlg]

/-- The stable order is antisymmetric. -/
theorem antisymm {f g : α → β} (hfg : StableLE f g) (hgf : StableLE g f) : f = g :=
  funext fun x => le_antisymm (hfg.le x) (hgf.le x)

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
theorem StableMap.stable (f : StableMap α β) : Stable f := f.2

/-- Berry's stable order makes stable maps a partial order. -/
instance : PartialOrder (StableMap α β) where
  le f g := StableLE f.1 g.1
  le_refl f := StableLE.refl f.2.monotone
  le_trans _ _ _ := StableLE.trans
  le_antisymm _ _ h h' := Subtype.ext (h.antisymm h')

/-- The stable order on `StableMap`. -/
theorem StableMap.le_iff {f g : StableMap α β} : f ≤ g ↔ StableLE f g := Iff.rfl

/-- The stable order is strictly finer than the pointwise order: on `Unit⊥`,
the identity lies pointwise below the constant `()`, but not in the stable
order, since `id ⊥ = ⊥` differs from `id () ⊓ const () ⊥ = ()`. -/
theorem Flat.not_stableLE_id_const :
    (∀ x : Part Unit, id x ≤ Part.some ()) ∧ ¬ StableLE (id : Part Unit → Part Unit)
      (fun _ => Part.some ()) := by
  refine ⟨fun x => ?_, fun h => ?_⟩
  · rcases Part.eq_none_or_eq_some x with h | ⟨u, h⟩ <;> simp [h]
  · have h' := (h.2 (bot_le : (⊥ : Part Unit) ≤ Part.some ())).unique
      (isGLB_pair_of_le le_rfl)
    exact Part.some_ne_none () h'.symm

end StableOrder

end Silmaril.Semantics.Domain
