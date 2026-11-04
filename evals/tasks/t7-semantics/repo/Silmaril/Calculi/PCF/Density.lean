module

public import Silmaril.Calculi.PCF.Definability

/-! # Definability of finite elements and density in PCF+`por`

We show that every monotone function of the environment which factors through the
`n`-th projections is definable by a PCF+`por` term (`DefinableAt`), given tests at
the types of the context:

* at a ground type (`definableAt_base`), the defining term is an if-chain over the
  finitely many non-`⊥` fixed values `v`, guarded by the parallel disjunction of the
  environment tests of the finitely many fixed environments `ē` with `g ē = v`;
* at an arrow type (`definableAt_arrow`), by currying.

Together with the tests of `Silmaril.Calculi.PCF.Definability`, an induction on types
(`hasTests_and_definableAt`) shows that every fixed point of a projection, i.e. every
finite element, is definable (`exists_denoteClosed_eq_proj`), so that every element of
the model is the supremum of a chain of definable elements (`exists_chain_definable`).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {Γ : Ctx Base} {σ τ : Ty} {n : ℕ}

/-- Every monotone `g` that factors through the `n`-th projections is definable in `Γ`. -/
def DefinableAt (n : ℕ) (Γ : Ctx Base) (τ : Ty) : Prop :=
  ∀ g : (model true).Env Γ → (model true).ty τ, Monotone g →
    (∀ ρ, proj n τ (g (projEnv n ρ)) = g ρ) → ∃ t : Term true Γ τ, ∀ ρ, denote t ρ = g ρ

/-- A function factoring through the projections is invariant under projecting its
argument. -/
theorem apply_projEnv_of_proj {g : (model true).Env Γ → (model true).ty τ}
    (hproj : ∀ ρ, proj n τ (g (projEnv n ρ)) = g ρ) (ρ : (model true).Env Γ) :
    g (projEnv n ρ) = g ρ := sorry

/-- A function factoring through the projections has fixed values. -/
theorem proj_apply_of_proj {g : (model true).Env Γ → (model true).ty τ}
    (hproj : ∀ ρ, proj n τ (g (projEnv n ρ)) = g ρ) (ρ : (model true).Env Γ) :
    proj n τ (g ρ) = g ρ := sorry

/-- Ground definability: an if-chain over the non-`⊥` fixed values, guarded by
parallel disjunctions of environment tests. -/
theorem definableAt_base (hΓ : ∀ a (_ : Var Γ a), HasTests n a) (b : Base) :
    DefinableAt n Γ (.base b) := sorry

/-- Arrow definability, by currying. -/
theorem definableAt_arrow (h : DefinableAt n (σ :: Γ) τ) : DefinableAt n Γ (σ ⤳ τ) := sorry

end Silmaril.Calculi.PCF
