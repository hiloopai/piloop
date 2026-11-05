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

/-- Example 4: partial functions form a domain. -/
noncomputable abbrev partialFunDomain (X Y : Type) : DomainCat := DomainCat.of (X → Part Y)

end PartialFunctions

section Finite

/-- A finite poset with a greatest element and two incomparable minimal
elements (the Hasse diagram of Example 5). -/
abbrev Wedge := {s : Finset Bool // s.Nonempty}

/-- Example 5: `Wedge` is a cpo, since it is finite. -/
noncomputable instance : OmegaCompletePartialOrder Wedge := ofFinite

end Finite

section Naturals

/-- The chain `0 ⊑ 1 ⊑ 2 ⊑ …` in `ℕ`. -/
def natChain : Chain ℕ := ⟨OrderHom.id⟩

theorem nat_no_ωSup : ¬ ∃ a : ℕ, IsLUB (Set.range natChain) a := sorry

/-- The chain of finite elements `0 ⊑ 1 ⊑ 2 ⊑ …` in `Ω = ℕ∞`. -/
noncomputable def omegaChain : Chain ℕ∞ :=
  ⟨⟨fun n => (n : ℕ∞), fun _ _ h => ENat.natCast_le_natCast.2 h⟩⟩

/-- Example 8: `Ω` is a domain; its chain of finite elements has lub `ω = ⊤`. -/
theorem ωSup_omegaChain : ωSup omegaChain = ⊤ := sorry

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

end Naturals

section NonContinuous

/-- Example 12: collapse every finite element of `Ω` to `0`. -/
noncomputable def collapse (x : ℕ∞) : ℕ∞ := if x = ⊤ then ⊤ else 0

end NonContinuous

end Silmaril.Semantics.Domain
