module

public import Mathlib.Data.Set.Basic
public import Mathlib.Data.Set.Insert

/-! # Exercise sheet 4, question 8 (region effects)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 8.


Answer: `Derives`. The reference type records its region, `intref ρ`; the
allocation rule gives `ref_ρ e` the type `intref ρ` and adds `A_ρ`; reading
`!e` and writing `e₁ := e₂` at type `intref ρ` add `R_ρ` and `W_ρ`; and function
types carry latent region effects. The sheet's example has type `int` and
effect `{A_ρ₁, R_ρ₁}` (`example_effect`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q8

/-- Region names. -/
abbrev Region := ℕ

/-- Region-annotated effects. -/
inductive Eff where
  | alloc (ρ : Region)
  | read (ρ : Region)
  | write (ρ : Region)

/-- Types `int | intref ρ | τ →ᶠ τ`. -/
inductive Ty where
  | int
  | intref (ρ : Region)
  | arrow (a : Ty) (F : Set Eff) (b : Ty)

/-- Terms with references. -/
inductive Term where
  | var (x : String)
  | lam (x : String) (e : Term)
  | app (f a : Term)
  | ref (ρ : Region) (e : Term)
  | deref (e : Term)
  | assign (e₁ e₂ : Term)

/-- The region-annotated type and effect system. -/
inductive Derives : (String → Option Ty) → Term → Ty → Set Eff → Prop where
  | var {Γ x t} : Γ x = some t → Derives Γ (.var x) t ∅
  | lam {Γ x e t t' F} : Derives (Function.update Γ x (some t)) e t' F →
      Derives Γ (.lam x e) (.arrow t F t') ∅
  | app {Γ f a t t' F F' F''} : Derives Γ f (.arrow t F'' t') F → Derives Γ a t F' →
      Derives Γ (.app f a) t' (F ∪ F' ∪ F'')
  | ref {Γ ρ e F} : Derives Γ e .int F → Derives Γ (.ref ρ e) (.intref ρ) (F ∪ {.alloc ρ})
  | deref {Γ e ρ F} : Derives Γ e (.intref ρ) F → Derives Γ (.deref e) .int (F ∪ {.read ρ})
  | assign {Γ e₁ e₂ ρ F F'} : Derives Γ e₁ (.intref ρ) F → Derives Γ e₂ .int F' →
      Derives Γ (.assign e₁ e₂) .int (F ∪ F' ∪ {.write ρ})
  | sub {Γ e t F F'} : Derives Γ e t F → F ⊆ F' → Derives Γ e t F'

/-- `x : int ⊢ (λr → !r) (ref_ρ₁ x) : int, {A_ρ₁, R_ρ₁}`. -/
theorem example_effect :
    Derives (fun y => if y = "x" then some .int else none)
      (.app (.lam "r" (.deref (.var "r"))) (.ref 1 (.var "x"))) .int {.alloc 1, .read 1} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q8
