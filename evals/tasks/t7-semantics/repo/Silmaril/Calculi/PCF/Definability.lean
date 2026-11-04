module

public import Silmaril.Calculi.PCF.Projection
public import Silmaril.Calculi.PCF.Parallel

/-! # Definability of finite elements in PCF+`por`

Towards full abstraction of the PCF+`por` model, we show that the fixed points of the
finite projections `proj n τ` are definable. This file first builds *three-valued
tests*: for a fixed element `e = proj n τ e`, a closed term `T : τ → bool` with

* `⟦T⟧ x = true` exactly when `e ⊑ proj n τ x`, and
* `⟦T⟧ x = false` whenever `e` and `proj n τ x` have no common upper bound.

The `false` clause is essential: tests which only answer `true` or `⊥` do not
compose at arrow types. Tests exist at the base types (`hasTests_base`), and at an
arrow type `σ → τ` as soon as the fixed points of `proj n σ` are definable and tests
exist at `τ` (`hasTests_arrow`); the arrow test is the parallel conjunction over the
finitely many fixed points `d` of `proj n σ` of the test for `e d` applied to `x d`.
Bounded completeness of `⟦τ⟧` is what makes a failure of compatibility visible at a
single such `d`.

Tests at the types of a context combine into *environment tests* (`exists_envTest`).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {Γ : Ctx Base} {σ τ : Ty} {n : ℕ}

/-- A three-valued test for `e` relative to the `n`-th projection: `⟦T⟧ x` is `true`
exactly when `e ⊑ proj n τ x`, and `false` when `e` and `proj n τ x` are
incompatible. -/
def IsTest (n : ℕ) {τ : Ty} (e : (model true).ty τ) (T : Closed true (τ ⤳ Ty.bool)) : Prop :=
  ∀ x, ((Model.app (denoteClosed T) x : Part Bool) = .some true ↔ e ≤ proj n τ x) ∧
    (¬ BddAbove ({e, proj n τ x} : Set _) →
      (Model.app (denoteClosed T) x : Part Bool) = .some false)

/-- Every fixed point of the `n`-th projection at `τ` has a test. -/
def HasTests (n : ℕ) (τ : Ty) : Prop := ∀ e, proj n τ e = e → ∃ T, IsTest n e T

/-- `⊥` is tested by the constant `true`. -/
theorem isTest_bot (n : ℕ) (τ : Ty) :
    IsTest n (⊥ : (model true).ty τ) (.lam Term.tt) := sorry

/-- A numeral `k < n` is tested by `eqNum k`. -/
theorem isTest_nat_some {k : ℕ} (hk : k < n) :
    IsTest (τ := Ty.nat) n (Part.some k : Part ℕ) (.lam (Term.eqNum k Term.v0)) := sorry

/-- `true` is tested by the identity. -/
theorem isTest_true (n : ℕ) :
    IsTest (τ := Ty.bool) n (Part.some true : Part Bool) (.lam Term.v0) := sorry

/-- `false` is tested by negation. -/
theorem isTest_false (n : ℕ) :
    IsTest (τ := Ty.bool) n (Part.some false : Part Bool) (.lam (Term.not Term.v0)) := sorry

/-- Every fixed point of a projection at a base type has a test. -/
theorem hasTests_base (n : ℕ) (b : Base) : HasTests n (.base b) := sorry

/-- Tests at an arrow type `σ → τ`, from definability of the fixed points at `σ` and
tests at `τ`: the parallel conjunction, over the fixed points `d` of `proj n σ`, of the
test for `e d` applied to `x d`. -/
theorem hasTests_arrow (hσ : ∀ d, proj n σ d = d → ∃ u : Closed true σ, denoteClosed u = d)
    (hτ : HasTests n τ) : HasTests n (σ ⤳ τ) := sorry

/-- Environment tests: given tests at the types of `Γ`, every fixed environment `ē`
has a test `Γ ⊢ t : bool`, which is `true` at `ρ` exactly when `ē ⊑ projEnv n ρ`, and
`false` when some coordinate of `ē` is incompatible with that of `projEnv n ρ`. -/
theorem exists_envTest (hΓ : ∀ a (_ : Var Γ a), HasTests n a) (ē : (model true).Env Γ) :
    ∃ t : Term true Γ .bool, projEnv n ē = ē → ∀ ρ,
      ((denote t ρ : Part Bool) = .some true ↔ ē ≤ projEnv n ρ) ∧
      ((∃ a x, ¬ BddAbove ({ē a x, proj n a (ρ a x)} : Set _)) →
        (denote t ρ : Part Bool) = .some false) := sorry

end Silmaril.Calculi.PCF
