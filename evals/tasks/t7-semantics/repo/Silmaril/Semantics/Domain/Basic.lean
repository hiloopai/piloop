module

public import Mathlib.Data.Fintype.Card
public import Mathlib.Order.Preorder.Finite
public import Mathlib.Data.Set.Finite.Range
public import Mathlib.Order.OrderIsoNat
public import Silmaril.Semantics.Domain.Chain

/-! # Cpos and domains

Definition 7 of the notes (a *cpo*, i.e. a chain-complete poset) is Mathlib's
`OmegaCompletePartialOrder`. Mathlib records the lub `ωSup` as data, but since
lubs are unique (Remark 1 (iv)) an ω-cpo structure is determined by its order:
`ofChainComplete` builds one from the bare existence of lubs. Definition 8 (a
*domain*, i.e. a cpo with a least element) is the unbundled pair
`[OmegaCompletePartialOrder D] [OrderBot D]`, or the bundled `DomainCat`.
-/

@[expose] public section

universe u

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D : Type*}

/-- Definition 7: a poset in which every chain has a lub is an ω-cpo. -/
noncomputable abbrev ofChainComplete [PartialOrder D]
    (h : ∀ c : Chain D, ∃ a, IsLUB (Set.range c) a) : OmegaCompletePartialOrder D where
  ωSup c := Classical.choose (h c)
  le_ωSup c n := (Classical.choose_spec (h c)).1 ⟨n, rfl⟩
  ωSup_le c _ hx := (Classical.choose_spec (h c)).2 (by rintro _ ⟨n, rfl⟩; exact hx n)

/-- A chain with finite range is eventually constant. -/
theorem Chain.eventuallyConstant_of_finite_range [PartialOrder D] {c : Chain D}
    (h : (Set.range c).Finite) : ∃ N, Chain.EventuallyConstantFrom c N := by
  obtain ⟨_, ⟨N, rfl⟩, hmax⟩ := h.exists_maximal (Set.range_nonempty c)
  refine ⟨N, fun n hn => le_antisymm ?_ (c.monotone hn)⟩
  exact hmax ⟨n, rfl⟩ (c.monotone hn)

/-- Every chain in a finite poset is eventually constant. -/
theorem Chain.eventuallyConstant_of_finite [PartialOrder D] [Finite D] (c : Chain D) :
    ∃ N, Chain.EventuallyConstantFrom c N :=
  Chain.eventuallyConstant_of_finite_range (Set.toFinite _)

/-- In an order with no infinite strictly ascending chain, every chain is
eventually constant. -/
theorem Chain.eventuallyConstant_of_wellFoundedGT [PartialOrder D] [WellFoundedGT D]
    (c : Chain D) : ∃ N, Chain.EventuallyConstantFrom c N := by
  obtain ⟨N, hN⟩ := WellFoundedGT.monotone_chain_condition c.toOrderHom
  exact ⟨N, fun n hn => (hN n hn).symm⟩

/-- Example 5: every finite poset is a cpo. -/
noncomputable abbrev ofFinite [PartialOrder D] [Finite D] : OmegaCompletePartialOrder D :=
  ofChainComplete fun c =>
    let ⟨_, hN⟩ := Chain.eventuallyConstant_of_finite c
    ⟨_, Chain.isLUB_of_eventuallyConstant hN⟩

end Silmaril.Semantics.Domain

namespace Silmaril

open OmegaCompletePartialOrder

/-- Definition 8: a bundled domain, an ω-cpo with a least element. -/
structure DomainCat : Type (u + 1) where
  /-- The underlying set. -/
  carrier : Type u
  [cpo : OmegaCompletePartialOrder carrier]
  [bot : OrderBot carrier]

namespace DomainCat

instance : CoeSort DomainCat.{u} (Type u) := ⟨DomainCat.carrier⟩

attribute [instance] DomainCat.cpo DomainCat.bot

/-- Bundle a domain. -/
abbrev of (D : Type u) [OmegaCompletePartialOrder D] [OrderBot D] : DomainCat.{u} := ⟨D⟩

@[simp] theorem coe_of (D : Type u) [OmegaCompletePartialOrder D] [OrderBot D] :
    (of D : Type u) = D := rfl

end DomainCat

end Silmaril
