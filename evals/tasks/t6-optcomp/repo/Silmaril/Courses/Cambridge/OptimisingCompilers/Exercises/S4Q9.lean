module

public import Mathlib.Data.Set.Basic

/-! # Exercise sheet 4, question 9 (WebLambda: application and conditional)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 9.


The function expression and argument effects are named `E₂` and `E₃`
respectively below.

Answer: `Derives`. The application can run only where the function value can be
created (`E₂`), the argument can be evaluated (`E₃`) and the function body can
run (`E₁`): `E₁ ∩ E₂ ∩ E₃`. A conditional runs where its condition and both
branches can (`Derives.ite`); `let` where both parts can.

The syntax includes `e₁ ⊕ e₂` for question 11; its rule, and subtyping
(question 10), are added in `S4Q10` and `S4Q11`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q9

/-- Execution environments. -/
inductive Loc where
  | client | server
  deriving DecidableEq

/-- Types `α | τ₁ →ᴱ τ₂` (one primitive type `int`). -/
inductive Ty where
  | int
  | arrow (a : Ty) (E : Set Loc) (b : Ty)

/-- WebLambda terms, with `⊕` (question 11). Primitives are variables. -/
inductive Term where
  | var (x : String)
  | app (f a : Term)
  | lam (x : String) (e : Term)
  | letE (x : String) (e₁ e₂ : Term)
  | ite (c t e : Term)
  | oplus (e₁ e₂ : Term)

/-- Contexts. -/
abbrev Ctx := String → Option Ty

/-- The rules of the sheet and the answer to question 9. -/
inductive Derives : Ctx → Term → Ty → Set Loc → Prop where
  | var {Γ x t} : Γ x = some t → Derives Γ (.var x) t Set.univ
  | lam {Γ x e t t' E} : Derives (Function.update Γ x (some t)) e t' E →
      Derives Γ (.lam x e) (.arrow t E t') E
  | app {Γ f a t t' E₁ E₂ E₃} : Derives Γ f (.arrow t E₁ t') E₂ → Derives Γ a t E₃ →
      Derives Γ (.app f a) t' (E₁ ∩ E₂ ∩ E₃)
  | ite {Γ c e₁ e₂ t E E₁ E₂} : Derives Γ c .int E → Derives Γ e₁ t E₁ → Derives Γ e₂ t E₂ →
      Derives Γ (.ite c e₁ e₂) t (E ∩ E₁ ∩ E₂)
  | letE {Γ x e₁ e₂ t t' E₁ E₂} : Derives Γ e₁ t E₁ →
      Derives (Function.update Γ x (some t)) e₂ t' E₂ → Derives Γ (.letE x e₁ e₂) t' (E₁ ∩ E₂)

/-- The context of primitives: `window.alert : int →^{client} int` and
`console.write : int →^{server} int`, bound as variables. -/
def Γprim : Ctx := fun x =>
  if x = "window.alert" then some (.arrow .int {.client} .int)
  else if x = "console.write" then some (.arrow .int {.server} .int)
  else if x = "b" then some .int else none

/-- Applying `window.alert` to a variable can run only on the client. -/
theorem app_rule :
    Derives Γprim (.app (.var "window.alert") (.var "b")) .int ({.client} ∩ Set.univ ∩ Set.univ) := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q9
