module

public import Silmaril.Semantics.Domain.Function
public import Mathlib.Basic.Finite.Prod
public import Mathlib.Data.Fintype.Option
public import Mathlib.SetTheory.Cardinal.NatCard

/-! # Finite domains

§8.2 of the notes ("Undecidability of contextual equivalence", p. 68) uses
three facts about finite domains:

* the flat domain `X⊥` over a finite set is finite (`Flat.finite`, through
  Mathlib's classical `Part.equivOption : Part X ≃ Option X`);
* the continuous functions between finite ω-cpos form a finite set
  (`ContinuousHom.finite`): a continuous map is determined by its underlying
  function;
* in a finite domain every chain is eventually constant
  (`Chain.eventuallyConstant_of_finite`), so every monotone map out of it is
  continuous.

The last fact holds more generally when chains stabilize, which in Mathlib's
terms is `WellFoundedGT`. A finite partial order is `WellFoundedGT`
(Mathlib's `Finite.to_wellFoundedGT`), so `ωScottContinuous_of_monotone`
applies to finite domains directly (`ωScottContinuous_of_monotone_of_finite`).
For such domains, continuous and monotone maps are the same, as orders
(`continuousHomOrderIso`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

universe u

variable {D E : Type*}

/-- The flat domain `X⊥` over a finite set `X` is finite. -/
instance Flat.finite {X : Type u} [Finite X] : Finite (Part X) :=
  Finite.of_equiv _ Part.equivOption.symm

/-- The number of elements of a flat domain: `|X⊥| = |X| + 1`. -/
theorem Flat.card {X : Type u} [Finite X] : Nat.card (Part X) = Nat.card X + 1 := by
  rw [Nat.card_congr Part.equivOption, Finite.card_option]

/-- The continuous functions between finite ω-cpos form a finite set. -/
instance ContinuousHom.finite [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]
    [Finite D] [Finite E] : Finite (D →𝒄 E) :=
  Finite.of_injective (fun f : D →𝒄 E => (f : D → E)) DFunLike.coe_injective

section Monotone

variable [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- Monotone maps out of a finite ω-cpo are continuous. -/
theorem ωScottContinuous_of_monotone_of_finite [Finite D] {f : D → E} (hf : Monotone f) :
    ωScottContinuous f :=
  ωScottContinuous_of_monotone hf

/-- Out of an ω-cpo whose chains stabilize, continuous maps are exactly the
monotone maps: `(D →𝒄 E) ≅ (D →o E)` as partial orders. -/
noncomputable def continuousHomOrderIso [WellFoundedGT D] : (D →𝒄 E) ≃o (D →o E) where
  toFun f := f.toOrderHom
  invFun f := ContinuousHom.ofFun f (ωScottContinuous_of_monotone f.monotone)
  left_inv _ := rfl
  right_inv _ := rfl
  map_rel_iff' := Iff.rfl

@[simp] theorem continuousHomOrderIso_apply [WellFoundedGT D] (f : D →𝒄 E) (d : D) :
    continuousHomOrderIso f d = f d := rfl

@[simp] theorem continuousHomOrderIso_symm_apply [WellFoundedGT D] (f : D →o E) (d : D) :
    continuousHomOrderIso.symm f d = f d := rfl

end Monotone

end Silmaril.Semantics.Domain
