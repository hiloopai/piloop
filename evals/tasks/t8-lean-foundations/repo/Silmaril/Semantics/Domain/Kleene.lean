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

theorem kleeneChain_succ (f : D →o D) (n : ℕ) : kleeneChain f (n + 1) = f (kleeneChain f n) := sorry

/-- The least fixed point of a continuous function, `⨆ₙ fⁿ(⊥)`. -/
noncomputable def fix (f : D →𝒄 D) : D := ωSup (kleeneChain f)

theorem iterate_le_fix (f : D →𝒄 D) (n : ℕ) : f^[n] ⊥ ≤ fix f := sorry

/-- `fix f` is a fixed point. -/
theorem fix_eq (f : D →𝒄 D) : f (fix f) = fix f := sorry

/-- Property (lfp-least): `fix f` is below every pre-fixed point. -/
theorem fix_le {f : D →𝒄 D} {d : D} (h : f d ≤ d) : fix f ≤ d := sorry

/-- `fix` is monotone (first half of Proposition 16). -/
theorem fix_mono {f g : D →𝒄 D} (h : f ≤ g) : fix f ≤ fix g := sorry

/-- Proposition 16: the fixed point operator is continuous. -/
theorem ωScottContinuous_fix : ωScottContinuous (fix : (D →𝒄 D) → D) := sorry

/-- Proposition 16: the fixed point operator as a continuous map. -/
noncomputable def fixHom : (D →𝒄 D) →𝒄 D := ContinuousHom.ofFun fix ωScottContinuous_fix

@[simp] theorem fixHom_apply (f : D →𝒄 D) : fixHom f = fix f := sorry

/-- Mathlib's `Fix` instance on `α → Part β` is Kleene's construction. This restates
`fix_eq_partFix` through the `Fix` class, whose instance here is `Part.fix`. -/
theorem fix_eq_piFix {α β : Type*} (f : (α → Part β) →𝒄 (α → Part β)) :
    fix f = Fix.fix (⇑f) := sorry

/-- Mathlib's `Fix` instance on `Part α` is Kleene's construction. -/
theorem fix_eq_partInstFix {α : Type*} (f : Part α →𝒄 Part α) : fix f = Fix.fix (⇑f) := sorry

/-- On a complete lattice, `fix f` is the lattice supremum `⨆ₙ fⁿ(⊥)`. -/
theorem fix_eq_iSup_iterate {L : Type*} [CompleteLattice L] (f : L →𝒄 L) :
    fix f = ⨆ n, f^[n] ⊥ := sorry

end Silmaril.Semantics.Domain
