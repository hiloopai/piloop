module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q9

/-! # Exercise sheet 4, question 10 (WebLambda subtyping)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 10.


Answer: `Sub`. Here the annotation is the set of environments where a function
*may* run, so "more effects" means fewer environments: a function that runs in
`E` can be used where one running in `E′ ⊆ E` is expected. Function types are
contravariant in the argument and covariant in the result. `DerivesSub` adds
subsumption to the rules of `S4Q9`; with it the conditional of the question
type-checks at the common supertype (`if_example`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q10

open S4Q9

/-- Subtyping. -/
inductive Sub : Ty → Ty → Prop where
  | int : Sub .int .int
  | arrow {a a' b b' E E'} : Sub a' a → Sub b b' → E' ⊆ E → Sub (.arrow a E b) (.arrow a' E' b')

/-- The rules of `S4Q9` with subsumption. -/
inductive DerivesSub : Ctx → Term → Ty → Set Loc → Prop where
  | base {Γ e t E} : Derives Γ e t E → DerivesSub Γ e t E
  | var {Γ x t} : Γ x = some t → DerivesSub Γ (.var x) t Set.univ
  | lam {Γ x e t t' E} : DerivesSub (Function.update Γ x (some t)) e t' E →
      DerivesSub Γ (.lam x e) (.arrow t E t') E
  | app {Γ f a t t' E₁ E₂ E₃} : DerivesSub Γ f (.arrow t E₁ t') E₂ → DerivesSub Γ a t E₃ →
      DerivesSub Γ (.app f a) t' (E₁ ∩ E₂ ∩ E₃)
  | ite {Γ c e₁ e₂ t E E₁ E₂} : DerivesSub Γ c .int E → DerivesSub Γ e₁ t E₁ →
      DerivesSub Γ e₂ t E₂ → DerivesSub Γ (.ite c e₁ e₂) t (E ∩ E₁ ∩ E₂)
  | letE {Γ x e₁ e₂ t t' E₁ E₂} : DerivesSub Γ e₁ t E₁ →
      DerivesSub (Function.update Γ x (some t)) e₂ t' E₂ →
      DerivesSub Γ (.letE x e₁ e₂) t' (E₁ ∩ E₂)
  | sub {Γ e t t' E} : DerivesSub Γ e t E → Sub t t' → DerivesSub Γ e t' E

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q10
