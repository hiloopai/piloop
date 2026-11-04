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
    Ty.AllBase P (.base b) ↔ P b := Iff.rfl

@[simp] theorem Ty.allBase_arrow {B : Type} (P : B → Prop) (σ τ : Ty B) :
    Ty.AllBase P (σ ⤳ τ) ↔ Ty.AllBase P σ ∧ Ty.AllBase P τ := Iff.rfl

/-- If every base type satisfies `P`, so does every type. -/
theorem Ty.allBase_of_forall {B : Type} {P : B → Prop} (h : ∀ b, P b) :
    ∀ τ : Ty B, Ty.AllBase P τ
  | .base b => h b
  | .arrow σ τ => ⟨Ty.allBase_of_forall h σ, Ty.allBase_of_forall h τ⟩

section Finite

variable {B : Type} {base : B → DomainCat.{0}} {P : B → Prop}

/-- If the base types satisfying `P` denote finite domains, so does every type
built from them. -/
theorem Ty.finite_denote (hbase : ∀ b, P b → Finite (base b)) :
    ∀ {τ : Ty B}, Ty.AllBase P τ → Finite (Ty.denote base τ)
  | .base b, h => hbase b h
  | .arrow σ τ, h => by
      have := Ty.finite_denote hbase h.1
      have := Ty.finite_denote hbase h.2
      exact (ContinuousHom.finite : Finite (Ty.denote base σ →𝒄 Ty.denote base τ))

/-- If the base types satisfying `P` denote finite domains, the environments of
a context of types built from them form a finite set. -/
theorem finite_tyEnv (hbase : ∀ b, P b → Finite (base b)) {Γ : List (Ty B)}
    (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) : Finite (TyEnv base Γ) := by
  have : ∀ i : Fin Γ.length, Finite (Ty.denote base (Γ.get i)) := fun i =>
    Ty.finite_denote hbase (hΓ _ (List.get_mem Γ i))
  exact Finite.of_equiv _ (Tuple.equivFn (X := fun a => (Ty.denote base a : Type)) Γ).symm

end Finite

namespace Model

variable {S : Signature} (M : Model S) {P : S.Base → Prop}

/-- In a model whose base domains satisfying `P` are finite, `⟦τ⟧` is finite
for every type `τ` built from them. -/
theorem finite_ty (hbase : ∀ b, P b → Finite (M.base b)) {τ : Ty S.Base}
    (hτ : Ty.AllBase P τ) : Finite (M.ty τ) :=
  Ty.finite_denote hbase hτ

/-- In a model whose base domains satisfying `P` are finite, `⟦Γ⟧` is finite for
every context `Γ` of types built from them. -/
theorem finite_env (hbase : ∀ b, P b → Finite (M.base b)) {Γ : Ctx S.Base}
    (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) : Finite (M.Env Γ) :=
  finite_tyEnv hbase hΓ

/-- In a model whose base domains satisfying `P` are finite, the possible
denotations `⟦Γ⟧ →𝒄 ⟦τ⟧` of terms `Γ ⊢ t : τ` form a finite set when `Γ` and
`τ` are built from them. -/
theorem finite_denoteHom (hbase : ∀ b, P b → Finite (M.base b)) {Γ : Ctx S.Base}
    {τ : Ty S.Base} (hΓ : ∀ a ∈ Γ, Ty.AllBase P a) (hτ : Ty.AllBase P τ) :
    Finite (M.Env Γ →𝒄 M.ty τ) :=
  haveI := M.finite_env hbase hΓ
  haveI := M.finite_ty hbase hτ
  ContinuousHom.finite

end Model

namespace Observation

variable {S : Signature} {O : Observation S} {M : Model S} {Γ : Ctx S.Base} {τ : Ty S.Base}

/-- If evaluation is sound and adequate at ground types, every class of
contextually equivalent terms is determined by the denotation of any of its
members: choosing a term of each denotation reaches every class. -/
theorem SoundAdequate.exists_surjective_ctxQuot (h : O.SoundAdequate M) :
    ∃ g : Set.range (M.denote : Term S Γ τ → (M.Env Γ →𝒄 M.ty τ)) → O.CtxQuot Γ τ,
      Function.Surjective g := by
  refine ⟨fun d => Quotient.mk _ (Classical.choose d.2), ?_⟩
  refine Quotient.ind fun t => ⟨⟨M.denote t, t, rfl⟩, Quotient.sound ?_⟩
  exact h.ctxEquiv_of_denote_eq
    (Classical.choose_spec (⟨t, rfl⟩ : ∃ u, M.denote u = M.denote t))

/-- If evaluation is sound and adequate at ground types, there are at most as
many terms `Γ ⊢ t : τ` up to contextual equivalence as elements of
`⟦Γ⟧ →𝒄 ⟦τ⟧`, when the latter is finite. -/
theorem SoundAdequate.card_ctxQuot_le (h : O.SoundAdequate M)
    [Finite (M.Env Γ →𝒄 M.ty τ)] :
    Nat.card (O.CtxQuot Γ τ) ≤ Nat.card (M.Env Γ →𝒄 M.ty τ) := by
  obtain ⟨g, hg⟩ := h.exists_surjective_ctxQuot (Γ := Γ) (τ := τ)
  exact (Nat.card_le_card_of_surjective g hg).trans (Finite.card_subtype_le _)

/-- If evaluation is sound and adequate at ground types and `⟦Γ⟧ →𝒄 ⟦τ⟧` is
finite, there are finitely many terms `Γ ⊢ t : τ` up to contextual
equivalence. -/
theorem SoundAdequate.finite_ctxQuot (h : O.SoundAdequate M)
    [Finite (M.Env Γ →𝒄 M.ty τ)] : Finite (O.CtxQuot Γ τ) := by
  obtain ⟨g, hg⟩ := h.exists_surjective_ctxQuot (Γ := Γ) (τ := τ)
  exact Finite.of_surjective g hg

end Observation

end Silmaril.Calculi.TypedLambda
