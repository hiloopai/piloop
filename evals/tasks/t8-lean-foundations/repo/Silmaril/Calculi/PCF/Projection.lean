module

public import Silmaril.Calculi.PCF.Denotation
public import Silmaril.Semantics.Domain.FiniteProjection
public import Silmaril.Semantics.Domain.BoundedComplete

/-! # Finite projections on the PCF+`por` model

Every domain `⟦τ⟧` of the model of PCF+`por` carries an ω-chain of idempotent
deflations `proj n τ` with finitely many fixed points each, whose supremum is the
identity (`ωSup_chain_proj`). At `nat` the `n`-th projection truncates the
numerals `≥ n` to `⊥`; at `bool` it is the identity; at an arrow type it is
conjugation, `proj n (σ ⤳ τ) f = proj n τ ∘ f ∘ proj n σ` (`app_proj`, by `rfl`).

These projections are extended componentwise to environments (`projEnv`). We also
record that every `⟦τ⟧` is bounded complete (`boundedComplete_ty`), that the ground
domains are flat (`base_le_iff`), and that the non-`⊥` fixed points of a ground
projection are denoted by finitely many closed literals (`exists_values`).

These are the finite elements used in the proof that PCF+`por` is fully abstract.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The finite projections on each domain `⟦τ⟧` of the PCF+`por` model. -/
noncomputable def finProj : (τ : Ty) → FiniteProjections ((model true).ty τ)
  | .base .nat => FiniteProjections.nat
  | .base .bool => FiniteProjections.ofFinite
  | .arrow σ τ => (finProj σ).arrow (finProj τ)

/-- The `n`-th projection on `⟦τ⟧`. -/
noncomputable abbrev proj (n : ℕ) (τ : Ty) : (model true).ty τ →𝒄 (model true).ty τ :=
  (finProj τ).proj n

variable {Γ : Ctx Base} {σ τ : Ty} {n : ℕ}

/-- At `nat`, the `n`-th projection keeps exactly the numerals below `n`. -/
theorem proj_nat_some (n k : ℕ) : proj n .nat (Part.some k : Part ℕ) =
    if k < n then Part.some k else Part.none := sorry

/-- At `nat`, `⊥` is fixed by every projection. -/
theorem proj_nat_none (n : ℕ) : proj n .nat (Part.none : Part ℕ) = Part.none := sorry

/-- At `bool`, every projection is the identity. -/
theorem proj_bool (n : ℕ) (x : (model true).ty (.base .bool)) : proj n (.base .bool) x = x := sorry

/-- At an arrow type, the projection is conjugation by the projections. -/
theorem app_proj (f : (model true).ty (σ ⤳ τ)) (d : (model true).ty σ) :
    Model.app (proj n (σ ⤳ τ) f) d = proj n τ (Model.app f (proj n σ d)) := sorry

/-- Projections are idempotent. -/
theorem proj_proj (n : ℕ) (τ : Ty) (d : (model true).ty τ) :
    proj n τ (proj n τ d) = proj n τ d := sorry

/-- Each projection has finitely many fixed points. -/
theorem finite_fixed_proj (n : ℕ) (τ : Ty) : {d : (model true).ty τ | proj n τ d = d}.Finite := sorry

/-- Projection of environments, componentwise. -/
noncomputable def projEnv (n : ℕ) {Γ : Ctx Base} (ρ : (model true).Env Γ) :
    (model true).Env Γ :=
  fun a x => proj n a (ρ a x)

/-- `projEnv` acts componentwise. -/
@[simp] theorem projEnv_apply (ρ : (model true).Env Γ) (a : Ty) (x : Var Γ a) :
    projEnv n ρ a x = proj n a (ρ a x) := sorry

/-- The projection of environments is idempotent. -/
theorem projEnv_projEnv (n : ℕ) (ρ : (model true).Env Γ) :
    projEnv n (projEnv n ρ) = projEnv n ρ := sorry

/-- The projection of environments is a deflation. -/
theorem projEnv_le (n : ℕ) (ρ : (model true).Env Γ) : projEnv n ρ ≤ ρ := sorry

/-- The projection of environments is monotone. -/
theorem projEnv_mono (n : ℕ) : Monotone (projEnv n (Γ := Γ)) := sorry

/-- The projection of environments increases with `n`. -/
theorem projEnv_le_projEnv {m : ℕ} (h : n ≤ m) (ρ : (model true).Env Γ) :
    projEnv n ρ ≤ projEnv m ρ := sorry

/-- Projecting an extended environment projects the new value and the rest. -/
theorem projEnv_cons (d : (model true).ty σ) (ρ : (model true).Env Γ) :
    projEnv n (d ::ₜ ρ) = proj n σ d ::ₜ projEnv n ρ := sorry

/-- The head of a projected environment. -/
@[simp] theorem head_projEnv (ρ : (model true).Env (σ :: Γ)) :
    Tuple.head (projEnv n ρ) = proj n σ (Tuple.head ρ) := sorry

/-- The tail of a projected environment. -/
@[simp] theorem tail_projEnv (ρ : (model true).Env (σ :: Γ)) :
    Tuple.tail (projEnv n ρ) = projEnv n (Tuple.tail ρ) := sorry

/-- The projection of environments has finitely many fixed points. -/
theorem finite_fixed_projEnv (n : ℕ) :
    (Γ : Ctx Base) → {ρ : (model true).Env Γ | projEnv n ρ = ρ}.Finite := sorry

/-- Every domain of the model is bounded complete. -/
theorem boundedComplete_ty : (τ : Ty) → BoundedComplete ((model true).ty τ) := sorry

/-- The ground domains are flat. -/
theorem base_le_iff {b : Base} {x y : (model true).ty (.base b)} :
    x ≤ y ↔ x = ⊥ ∨ x = y := sorry

/-- The non-⊥ fixed points at a base type, each a closed literal. -/
theorem exists_values (n : ℕ) (b : Base) : ∃ V : List ((model true).ty (.base b)),
    (∀ v ∈ V, v ≠ ⊥ ∧ ∃ w : Closed true (.base b), denoteClosed w = v) ∧
    ∀ v, proj n (.base b) v = v → v ≠ ⊥ → v ∈ V := sorry

end Silmaril.Calculi.PCF
