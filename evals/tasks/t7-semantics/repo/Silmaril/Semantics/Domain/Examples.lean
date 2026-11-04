module

public import Silmaril.Semantics.Domain.Kleene
public import Mathlib.Data.ENat.Lattice
public import Mathlib.Data.Fintype.Powerset
public import Mathlib.Data.PFun

/-! # Examples of cpos and domains

The examples of §2 of the notes. The domain of partial functions `X ⇀ Y` is
`X → Part Y`, which is Mathlib's `X →. Y`: Mathlib orders it pointwise, and
`partialFun_le_iff` shows that this is inclusion of graphs (`PFun.graph`). The
vertical natural numbers `Ω = ω + 1` are `ℕ∞`.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

section PartialFunctions

variable {X Y : Type*}

/-- The graph of a partial function; Mathlib's `PFun.graph`, since `X → Part Y`
is `X →. Y`. -/
abbrev graph (f : X → Part Y) : Set (X × Y) := PFun.graph f

/-- Example 1: partial functions are ordered by inclusion of graphs. -/
theorem partialFun_le_iff {f g : X → Part Y} : f ≤ g ↔ graph f ⊆ graph g := by
  constructor
  · rintro h ⟨x, y⟩ hy
    exact h x y hy
  · intro h x y hy
    exact h (a := (x, y)) hy

/-- Example 4: the least partial function is nowhere defined. -/
theorem partialFun_bot_apply (x : X) : (⊥ : X → Part Y) x = Part.none := rfl

/-- Example 4: the lub of a chain of partial functions is their union. -/
theorem partialFun_mem_ωSup {c : Chain (X → Part Y)} {x : X} {y : Y} :
    y ∈ ωSup c x ↔ ∃ n, y ∈ c n x := by
  change y ∈ ωSup (c.map (Pi.evalOrderHom x)) ↔ _
  rw [Part.mem_ωSup]
  constructor
  · rintro ⟨n, hn⟩
    exact ⟨n, Part.eq_some_iff.1 hn.symm⟩
  · rintro ⟨n, hn⟩
    exact ⟨n, (Part.eq_some_iff.2 hn).symm⟩

/-- Theorem 6 on partial functions: outputs of the least fixed point are
outputs of some Kleene iterate. Compare Mathlib's `Part.Fix.mem_iff` via
`fix_eq_partFix`. -/
theorem partialFun_mem_fix_iff {f : (X → Part Y) →𝒄 (X → Part Y)} {x : X} {y : Y} :
    y ∈ fix f x ↔ ∃ n, y ∈ f^[n] ⊥ x :=
  partialFun_mem_ωSup

/-- Example 4: partial functions form a domain. -/
noncomputable abbrev partialFunDomain (X Y : Type) : DomainCat := DomainCat.of (X → Part Y)

end PartialFunctions

section Finite

/-- A finite poset with a greatest element and two incomparable minimal
elements (the Hasse diagram of Example 5). -/
abbrev Wedge := {s : Finset Bool // s.Nonempty}

/-- Example 5: `Wedge` is a cpo, since it is finite. -/
noncomputable instance : OmegaCompletePartialOrder Wedge := ofFinite

/-- Example 5: a finite cpo need not be a domain. -/
theorem wedge_not_isLeast : ¬ ∃ b : Wedge, IsLeast Set.univ b := by
  rintro ⟨⟨b, hb⟩, -, hle⟩
  have ht : b ⊆ {true} := hle (a := ⟨{true}, by simp⟩) trivial
  have hf : b ⊆ {false} := hle (a := ⟨{false}, by simp⟩) trivial
  obtain ⟨x, hx⟩ := hb
  have h1 := ht hx
  have h2 := hf hx
  simp only [Finset.mem_singleton] at h1 h2
  exact Bool.noConfusion (h1.symm.trans h2)

end Finite

section Naturals

/-- The chain `0 ⊑ 1 ⊑ 2 ⊑ …` in `ℕ`. -/
def natChain : Chain ℕ := ⟨OrderHom.id⟩

/-- Example 7: the chain `0 ≤ 1 ≤ 2 ≤ …` has no upper bound in `ℕ`, so `ℕ` is
not a cpo. -/
theorem nat_not_bddAbove : ¬ BddAbove (Set.range natChain) := by
  rintro ⟨b, hb⟩
  have h := hb ⟨b + 1, rfl⟩
  change b + 1 ≤ b at h
  omega

theorem nat_no_ωSup : ¬ ∃ a : ℕ, IsLUB (Set.range natChain) a :=
  fun ⟨a, ha⟩ => nat_not_bddAbove ⟨a, ha.1⟩

/-- The chain of finite elements `0 ⊑ 1 ⊑ 2 ⊑ …` in `Ω = ℕ∞`. -/
noncomputable def omegaChain : Chain ℕ∞ :=
  ⟨⟨fun n => (n : ℕ∞), fun _ _ h => ENat.natCast_le_natCast.2 h⟩⟩

/-- Example 8: `Ω` is a domain; its chain of finite elements has lub `ω = ⊤`. -/
theorem ωSup_omegaChain : ωSup omegaChain = ⊤ := by
  change ⨆ n : ℕ, (n : ℕ∞) = ⊤
  exact ENat.iSup_natCast

/-- Example 8: the vertical natural numbers as a domain. -/
noncomputable abbrev Omega : DomainCat := DomainCat.of ℕ∞

/-- Example 9: `ℕ` with two incomparable upper bounds `ω₁` and `ω₂`, as a
family of sets ordered by inclusion. -/
def twoTops : Set (Set (ℕ ⊕ Bool)) :=
  Set.range (fun n : ℕ => Sum.inl '' Set.Iio n) ∪
    {Set.range Sum.inl ∪ {Sum.inr true}, Set.range Sum.inl ∪ {Sum.inr false}}

/-- The elements of Example 9. -/
abbrev TwoTops := twoTops

/-- The element `n` of `TwoTops`. -/
def TwoTops.nat (n : ℕ) : TwoTops := ⟨Sum.inl '' Set.Iio n, Or.inl ⟨n, rfl⟩⟩

/-- The upper bound `ω₁` (for `b = true`) or `ω₂` (for `b = false`). -/
def TwoTops.top (b : Bool) : TwoTops :=
  ⟨Set.range Sum.inl ∪ {Sum.inr b}, Or.inr (by cases b <;> simp)⟩

/-- The chain `0 ⊑ 1 ⊑ 2 ⊑ …` in `TwoTops`. -/
def twoTopsChain : Chain TwoTops :=
  ⟨⟨TwoTops.nat, fun _ _ h => Set.image_mono (Set.Iio_subset_Iio h)⟩⟩

theorem TwoTops.top_mem_upperBounds (b : Bool) :
    TwoTops.top b ∈ upperBounds (Set.range twoTopsChain) := by
  rintro _ ⟨n, rfl⟩
  rintro _ ⟨k, -, rfl⟩
  exact Or.inl ⟨k, rfl⟩

theorem TwoTops.not_top_le (b : Bool) : ¬ TwoTops.top b ≤ TwoTops.top (!b) := by
  intro h
  have := h (a := Sum.inr b) (Or.inr rfl)
  rcases this with ⟨_, h⟩ | h
  · exact Sum.inl_ne_inr h
  · cases b <;> simp at h

/-- Example 9: the chain has upper bounds but no least one, so `TwoTops` is not
a cpo. -/
theorem twoTops_no_lub : ¬ ∃ a : TwoTops, IsLUB (Set.range twoTopsChain) a := by
  rintro ⟨⟨a, ha⟩, hlub⟩
  have hle : ∀ b, (⟨a, ha⟩ : TwoTops) ≤ TwoTops.top b :=
    fun b => hlub.2 (TwoTops.top_mem_upperBounds b)
  rcases ha with ⟨n, rfl⟩ | ha
  · have hn := hlub.1 ⟨n + 1, rfl⟩
    have : Sum.inl n ∈ Sum.inl '' Set.Iio n := hn (a := Sum.inl n) ⟨n, by simp, rfl⟩
    simp at this
  · rcases ha with rfl | rfl
    · exact TwoTops.not_top_le true (hle false)
    · exact TwoTops.not_top_le false (hle true)

end Naturals

section NonContinuous

/-- Example 12: collapse every finite element of `Ω` to `0`. -/
noncomputable def collapse (x : ℕ∞) : ℕ∞ := if x = ⊤ then ⊤ else 0

theorem collapse_monotone : Monotone collapse := by
  intro x y hxy
  unfold collapse
  split_ifs with hx hy <;> simp_all

theorem collapse_strict : Strict collapse := by
  simp [Strict, collapse]

/-- Example 12: `collapse` is monotone and strict but not continuous. -/
theorem collapse_not_continuous : ¬ ωScottContinuous collapse := by
  intro h
  have := h.map_ωSup omegaChain
  rw [ωSup_omegaChain] at this
  have hz : ωSup (omegaChain.map ⟨collapse, h.monotone⟩) = 0 := by
    refine le_antisymm (ωSup_le _ _ fun n => ?_) bot_le
    change collapse (n : ℕ∞) ≤ 0
    simp [collapse]
  simp [hz, collapse] at this

end NonContinuous

end Silmaril.Semantics.Domain
