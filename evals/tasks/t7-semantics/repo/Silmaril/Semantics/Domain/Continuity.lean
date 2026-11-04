module

public import Silmaril.Semantics.Domain.Basic
public import Mathlib.Order.WellFounded
public import Mathlib.Order.Hom.Bounded

/-! # Continuous functions

Definition 9 of the notes (§2.5) is Mathlib's `ωScottContinuous`, bundled as
`α →𝒄 β`. This module records the notes' formulation (monotone and preserving
lubs of chains), strictness (Definition 10), Remark 2, and constant functions
(Example 10).

It also proves that every monotone function out of an ω-cpo whose chains
stabilize is continuous. In Mathlib terms, chains stabilize exactly when the
order is `WellFoundedGT`. This covers finite cpos, flat domains and their
finite products, so it discharges the continuity obligations for `if` and
parallel or.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D E : Type*}

section Continuity

variable [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E] {f : D → E}

/-- Definition 9: a function is continuous iff it is monotone and preserves lubs
of chains (Mathlib `ωScottContinuous_iff_monotone_map_ωSup`). -/
alias ωScottContinuous_iff := ωScottContinuous_iff_monotone_map_ωSup

/-- Remark 2: a monotone function always maps a lub above the lub of the image. -/
theorem ωSup_map_le_of_monotone (hf : Monotone f) (c : Chain D) :
    ωSup (c.map ⟨f, hf⟩) ≤ f (ωSup c) :=
  ωSup_le _ _ fun n => hf (le_ωSup c n)

/-- Remark 2: to prove a monotone function continuous it suffices to prove one
inequality. -/
theorem ωScottContinuous_of_le (hf : Monotone f)
    (h : ∀ c : Chain D, f (ωSup c) ≤ ωSup (c.map ⟨f, hf⟩)) : ωScottContinuous f :=
  ωScottContinuous.of_monotone_map_ωSup
    ⟨hf, fun c => le_antisymm (h c) (ωSup_map_le_of_monotone hf c)⟩

/-- Example 10: constant functions are continuous. This is Mathlib's
`ωScottContinuous.const` with the constant made explicit. -/
theorem ωScottContinuous_const (e : E) : ωScottContinuous (fun _ : D => e) :=
  ωScottContinuous.const

/-- A continuous function as a bundled `→𝒄` (Mathlib `ContinuousHom.ofFun`). -/
abbrev toContinuousHom (f : D → E) (hf : ωScottContinuous f) : D →𝒄 E :=
  ContinuousHom.ofFun f hf

end Continuity

section Strict

/-- Definition 10: `f` is strict when it preserves the least element. -/
def Strict [Bot D] [Bot E] (f : D → E) : Prop := f ⊥ = ⊥

theorem strict_id [Bot D] : Strict (id : D → D) := rfl

theorem Strict.comp {F : Type*} [Bot D] [Bot E] [Bot F] {g : E → F} {f : D → E}
    (hg : Strict g) (hf : Strict f) : Strict (g ∘ f) := by
  simp only [Strict] at hf hg ⊢
  rw [Function.comp_apply, hf, hg]

/-- A strict function bundled as a Mathlib `BotHom`. -/
def Strict.toBotHom [Bot D] [Bot E] {f : D → E} (h : Strict f) : BotHom D E := ⟨f, h⟩

/-- The underlying function of `Strict.toBotHom` is the original function. -/
@[simp] theorem Strict.coe_toBotHom [Bot D] [Bot E] {f : D → E} (h : Strict f) :
    ⇑h.toBotHom = f := rfl

/-- Every `⊥`-preserving bundled map (e.g. a Mathlib `BotHom`) is strict. -/
theorem strict_of_botHomClass {F : Type*} [Bot D] [Bot E] [FunLike F D E]
    [BotHomClass F D E] (f : F) : Strict f := map_bot f

/-- Strict functions are exactly the underlying functions of Mathlib `BotHom`s. -/
theorem strict_iff_exists_botHom [Bot D] [Bot E] {f : D → E} :
    Strict f ↔ ∃ g : BotHom D E, ⇑g = f :=
  ⟨fun h => ⟨h.toBotHom, rfl⟩, fun ⟨g, hg⟩ => hg ▸ strict_of_botHomClass g⟩

end Strict

section Stable

variable [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- Monotone functions out of an ω-cpo with stabilizing chains are continuous. -/
theorem ωScottContinuous_of_monotone [WellFoundedGT D] {f : D → E} (hf : Monotone f) :
    ωScottContinuous f := by
  refine ωScottContinuous_of_le hf fun c => ?_
  obtain ⟨N, hN⟩ := Chain.eventuallyConstant_of_wellFoundedGT c
  rw [Chain.ωSup_eq_of_eventuallyConstant hN]
  exact le_ωSup (c.map ⟨f, hf⟩) N

end Stable

end Silmaril.Semantics.Domain
