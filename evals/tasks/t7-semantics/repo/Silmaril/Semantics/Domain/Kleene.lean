module

public import Silmaril.Semantics.Domain.Continuity
public import Silmaril.Semantics.Domain.Order
public import Mathlib.Control.LawfulFix

/-! # Kleene's fixed point theorem

Theorem 6 of the notes (§2.6): a continuous function `f` on a domain has a
least pre-fixed point `fix f = ⨆ₙ fⁿ(⊥)`, which is also its least fixed point.
Proposition 16 (§3.3): `fix` is itself continuous on the function domain.

For partial-function domains `(a : α) → Part (β a)` the construction agrees
with Mathlib's `Part.fix` (`fix_eq_partFix`), which Mathlib defines by a
separate approximation argument; hence also with Mathlib's `Fix` instances on
`α → Part β` and `Part α` (`fix_eq_piFix`, `fix_eq_partInstFix`). In general
`fix` lies below the fixed point of any `LawfulFix` instance
(`fix_le_lawfulFix`), and on a complete lattice it is `OrderHom.lfp`
(`fix_eq_lfp`, `fix_eq_iSup_iterate`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D : Type*} [OmegaCompletePartialOrder D] [OrderBot D]

/-- The Kleene chain `⊥ ⊑ f(⊥) ⊑ f²(⊥) ⊑ …` of a monotone function. -/
def kleeneChain (f : D →o D) : Chain D := fixedPoints.iterateChain f ⊥ bot_le

@[simp] theorem kleeneChain_apply (f : D →o D) (n : ℕ) : kleeneChain f n = f^[n] ⊥ := rfl

theorem kleeneChain_succ (f : D →o D) (n : ℕ) : kleeneChain f (n + 1) = f (kleeneChain f n) :=
  Function.iterate_succ_apply' f n ⊥

/-- The least fixed point of a continuous function, `⨆ₙ fⁿ(⊥)`. -/
noncomputable def fix (f : D →𝒄 D) : D := ωSup (kleeneChain f)

theorem iterate_le_fix (f : D →𝒄 D) (n : ℕ) : f^[n] ⊥ ≤ fix f :=
  le_ωSup (kleeneChain (f : D →o D)) n

/-- `fix f` is a fixed point. -/
theorem fix_eq (f : D →𝒄 D) : f (fix f) = fix f :=
  fixedPoints.ωSup_iterate_mem_fixedPoint f ⊥ bot_le

/-- Property (lfp-least): `fix f` is below every pre-fixed point. -/
theorem fix_le {f : D →𝒄 D} {d : D} (h : f d ≤ d) : fix f ≤ d :=
  fixedPoints.ωSup_iterate_le_prefixedPoint f ⊥ bot_le h bot_le

/-- Theorem 6 (Kleene): `fix f` is the least pre-fixed point of `f`. -/
theorem isLeastPrefixedPoint_fix (f : D →𝒄 D) : IsLeastPrefixedPoint f (fix f) :=
  IsLeastPrefixedPoint.of_le (fix_eq f).le fun _ => fix_le

/-- Theorem 6 with Proposition 1: `fix f` is the least fixed point of `f`. -/
theorem isLeast_fixedPoints_fix (f : D →𝒄 D) : IsLeast (Function.fixedPoints f) (fix f) :=
  (isLeastPrefixedPoint_fix f).isLeast_fixedPoints f.monotone

/-- `fix` is monotone (first half of Proposition 16). -/
theorem fix_mono {f g : D →𝒄 D} (h : f ≤ g) : fix f ≤ fix g :=
  fix_le ((h (fix g)).trans (fix_eq g).le)

/-- Proposition 16: the fixed point operator is continuous. -/
theorem ωScottContinuous_fix : ωScottContinuous (fix : (D →𝒄 D) → D) := by
  refine ωScottContinuous_of_le (fun _ _ h => fix_mono h) fun c => fix_le ?_
  -- `⨆ₙ fix(fₙ)` is a pre-fixed point of `⨆ₘ fₘ`.
  simp only [ContinuousHom.ωSup_def, ContinuousHom.ωSup_apply]
  refine ωSup_le _ _ fun m => ?_
  change c m (ωSup _) ≤ _
  rw [(c m).continuous]
  refine ωSup_le _ _ fun n => ?_
  change c m (fix (c n)) ≤ ωSup (c.map ⟨fix, fun _ _ h => fix_mono h⟩)
  calc c m (fix (c n)) ≤ c (max m n) (fix (c (max m n))) :=
        ContinuousHom.apply_mono (c.monotone (le_max_left m n))
          (fix_mono (c.monotone (le_max_right m n)))
    _ = fix (c (max m n)) := fix_eq _
    _ ≤ _ := le_ωSup (c.map ⟨fix, fun _ _ h => fix_mono h⟩) (max m n)

/-- Proposition 16: the fixed point operator as a continuous map. -/
noncomputable def fixHom : (D →𝒄 D) →𝒄 D := ContinuousHom.ofFun fix ωScottContinuous_fix

@[simp] theorem fixHom_apply (f : D →𝒄 D) : fixHom f = fix f := rfl

/-- Kleene's construction agrees with Mathlib's `Part.fix` on partial-function
domains. -/
theorem fix_eq_partFix {α : Type*} {β : α → Type*}
    (f : ((a : α) → Part (β a)) →𝒄 ((a : α) → Part (β a))) :
    fix f = Part.fix f := by
  rw [Part.fix_eq_ωSup_of_ωScottContinuous f.ωScottContinuous]
  refine le_antisymm (ωSup_le _ _ fun n => le_ωSup_of_le n ?_)
    (ωSup_le _ _ fun n => le_ωSup_of_le n ?_) <;>
  · induction n with
    | zero => exact le_rfl
    | succ n ih =>
        simp only [kleeneChain_apply, Function.iterate_succ_apply'] at ih ⊢
        exact f.monotone ih

/-- Mathlib's `Fix` instance on `α → Part β` is Kleene's construction. This restates
`fix_eq_partFix` through the `Fix` class, whose instance here is `Part.fix`. -/
theorem fix_eq_piFix {α β : Type*} (f : (α → Part β) →𝒄 (α → Part β)) :
    fix f = Fix.fix (⇑f) :=
  fix_eq_partFix f

/-- Kleene's fixed point lies below the fixed point chosen by any lawful `Fix`
instance. -/
theorem fix_le_lawfulFix [LawfulFix D] {f : D → D} (h : ωScottContinuous f) :
    fix (ContinuousHom.ofFun f h) ≤ Fix.fix f :=
  fix_le (LawfulFix.fix_eq h).symm.le

/-- Mathlib's `Fix` instance on `Part α` is Kleene's construction. -/
theorem fix_eq_partInstFix {α : Type*} (f : Part α →𝒄 Part α) : fix f = Fix.fix (⇑f) := by
  refine le_antisymm (fix_le_lawfulFix f.ωScottContinuous) ?_
  change Part.fix (Part.toUnitMono (f : Part α →o Part α)) () ≤ fix f
  exact Part.fix_le (Part.toUnitMono (f : Part α →o Part α)) (X := fun _ => fix f)
    (fun _ => (fix_eq f).le) ()

/-- On a complete lattice, Kleene's construction agrees with Mathlib's `OrderHom.lfp`. -/
theorem fix_eq_lfp {L : Type*} [CompleteLattice L] (f : L →𝒄 L) :
    fix f = (f : L →o L).lfp :=
  (isLeastPrefixedPoint_fix f).eq_lfp

/-- On a complete lattice, `fix f` is the lattice supremum `⨆ₙ fⁿ(⊥)`. -/
theorem fix_eq_iSup_iterate {L : Type*} [CompleteLattice L] (f : L →𝒄 L) :
    fix f = ⨆ n, f^[n] ⊥ :=
  (fix_eq_lfp f).trans (fixedPoints.lfp_eq_sSup_iterate _ f.ωScottContinuous)

end Silmaril.Semantics.Domain
