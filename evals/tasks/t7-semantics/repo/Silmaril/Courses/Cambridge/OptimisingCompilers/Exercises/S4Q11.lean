module

public import Mathlib.Data.Set.Insert
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q10

/-! # Exercise sheet 4, question 11 (WebLambda `⊕`)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 11.


Answer: `Derives⊕` adds the rule
`Γ ⊢ e₁ : τ₁ →ᴱ¹ τ₂, E    Γ ⊢ e₂ : τ₁ →ᴱ² τ₂, E  ⟹  Γ ⊢ e₁ ⊕ e₂ : τ₁ →^{E₁ ∪ E₂} τ₂, E`:
the combined function runs wherever either component does, and is created where
both components are. `alert_oplus_write`: `window.alert ⊕ console.write` runs
on both sides.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q11

open S4Q9 S4Q10

/-- The full system: subsumption and `⊕`. -/
inductive DerivesOplus : Ctx → Term → Ty → Set Loc → Prop where
  | base {Γ e t E} : DerivesSub Γ e t E → DerivesOplus Γ e t E
  | oplus {Γ e₁ e₂ t t' E E₁ E₂} : DerivesOplus Γ e₁ (.arrow t E₁ t') E →
      DerivesOplus Γ e₂ (.arrow t E₂ t') E → DerivesOplus Γ (.oplus e₁ e₂) (.arrow t (E₁ ∪ E₂) t') E
  | app {Γ f a t t' E₁ E₂ E₃} : DerivesOplus Γ f (.arrow t E₁ t') E₂ → DerivesOplus Γ a t E₃ →
      DerivesOplus Γ (.app f a) t' (E₁ ∩ E₂ ∩ E₃)
  | ite {Γ c e₁ e₂ t E E₁ E₂} : DerivesOplus Γ c .int E → DerivesOplus Γ e₁ t E₁ →
      DerivesOplus Γ e₂ t E₂ → DerivesOplus Γ (.ite c e₁ e₂) t (E ∩ E₁ ∩ E₂)
  | lam {Γ x e t t' E} : DerivesOplus (Function.update Γ x (some t)) e t' E →
      DerivesOplus Γ (.lam x e) (.arrow t E t') E
  | sub {Γ e t t' E} : DerivesOplus Γ e t E → Sub t t' → DerivesOplus Γ e t' E

/-- `window.alert ⊕ console.write : int →^{client, server} int`. -/
theorem alert_oplus_write :
    DerivesOplus Γprim (.oplus (.var "window.alert") (.var "console.write"))
      (.arrow .int Set.univ .int) Set.univ := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q11
