module

public import Silmaril.Semantics.Domain.Finite
public import Silmaril.Calculi.TypedLambda.Denotation

/-! # Finite models

When the base domains of a model are finite, so is the domain denoted by
every type built from them: a function domain between finite domains is finite
(`Silmaril.Semantics.Domain.ContinuousHom.finite`). More precisely, only the base types that
actually occur in a type matter (`Ty.AllBase`), so a model may have infinite
base domains, like `ℕ⊥` for PCF, and still be finite on a fragment, like the
types of PCF_bool built from `𝔹⊥` alone (§8.2 of the notes, p. 68).

Theorem 24 (`Observation.SoundAdequate.ctxEquiv_of_denote_eq`) then bounds
the number of terms up to contextual equivalence (`Observation.CtxQuot`):
every class contains a term of each denotation it meets, so there are at most
as many classes as elements of `⟦Γ⟧ →𝒄 ⟦τ⟧`
(`Observation.SoundAdequate.card_ctxQuot_le`). In particular they are finitely
many when that domain is finite (`Observation.SoundAdequate.finite_ctxQuot`).
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open OmegaCompletePartialOrder Silmaril.Semantics.Domain Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- `Ty.AllBase P τ`: every base type occurring in `τ` satisfies `P`. -/
def Ty.AllBase {B : Type} (P : B → Prop) : Ty B → Prop
  | .base b => P b
  | .arrow σ τ => Ty.AllBase P σ ∧ Ty.AllBase P τ

@[simp] theorem Ty.allBase_base {B : Type} (P : B → Prop) (b : B) :
    Ty.AllBase P (.base b) ↔ P b := sorry

@[simp] theorem Ty.allBase_arrow {B : Type} (P : B → Prop) (σ τ : Ty B) :
    Ty.AllBase P (σ ⤳ τ) ↔ Ty.AllBase P σ ∧ Ty.AllBase P τ := sorry

/-- If every base type satisfies `P`, so does every type. -/
theorem Ty.allBase_of_forall {B : Type} {P : B → Prop} (h : ∀ b, P b) :
    ∀ τ : Ty B, Ty.AllBase P τ := sorry

section Finite

variable {B : Type} {base : B → DomainCat.{0}} {P : B → Prop}

/-- If the base types satisfying `P` denote finite domains, so does every type
built from them. -/
theorem Ty.finite_denote (hbase : ∀ b, P b → Finite (base b)) :
    ∀ {τ : Ty B}, Ty.AllBase P τ → Finite (Ty.denote base τ) := sorry

/-- If the base types satisfying `P` denote finite domains, the environments of
a context of types built from them form a finite set. -/
theorem finite_tyEnv (hbase : ∀ b, P b → Finite (base b)) {Γ : List (Ty B)}
    (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) : Finite (TyEnv base Γ) := sorry

end Finite

namespace Model

variable {S : Signature} (M : Model S) {P : S.Base → Prop}

/-- In a model whose base domains satisfying `P` are finite, `⟦τ⟧` is finite
for every type `τ` built from them. -/
theorem finite_ty (hbase : ∀ b, P b → Finite (M.base b)) {τ : Ty S.Base}
    (hτ : Ty.AllBase P τ) : Finite (M.ty τ) := sorry

/-- In a model whose base domains satisfying `P` are finite, `⟦Γ⟧` is finite for
every context `Γ` of types built from them. -/
theorem finite_env (hbase : ∀ b, P b → Finite (M.base b)) {Γ : Ctx S.Base}
    (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) : Finite (M.Env Γ) := sorry

/-- In a model whose base domains satisfying `P` are finite, the possible
denotations `⟦Γ⟧ →𝒄 ⟦τ⟧` of terms `Γ ⊢ t : τ` form a finite set when `Γ` and
`τ` are built from them. -/
theorem finite_denoteHom (hbase : ∀ b, P b → Finite (M.base b)) {Γ : Ctx S.Base}
    {τ : Ty S.Base} (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) (hτ : Ty.AllBase P τ) :
    Finite (M.Env Γ →𝒄 M.ty τ) := sorry

end Model

namespace Observation

variable {S : Signature} {O : Observation S} {M : Model S} {Γ : Ctx S.Base} {τ : Ty S.Base}

end Observation

end Silmaril.Calculi.TypedLambda
