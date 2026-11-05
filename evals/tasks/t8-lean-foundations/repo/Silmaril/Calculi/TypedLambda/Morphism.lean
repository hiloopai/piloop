module

public import Silmaril.Calculi.TypedLambda.Substitution

/-! # Signature morphisms

A translation of operators between two signatures over the same base types
induces a translation of terms that is the identity on variables, abstraction
and application (`Term.mapOp`). More generally, each operator may be
interpreted by a *derived operator*: a term of the target signature over the
operator's argument variables (`Term.translate`). Both commute with renaming
and substitution.

For example, these interfaces can express the inclusion of PCF into PCF+`por`
as an operator translation, and replacing parallel or by sequential or as a
derived-operator translation in the other direction. They do not assert that
such a replacement preserves observations.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda

variable {S : Signature} {Op' : List (Ty S.Base) → Ty S.Base → Type}

/-- A signature with the base types of `S` and the operators `Op'`. -/
abbrev Signature.withOps (S : Signature) (Op' : List (Ty S.Base) → Ty S.Base → Type) :
    Signature :=
  ⟨S.Base, Op'⟩

/-- A translation of operators that preserves arities. -/
abbrev OpMap (S : Signature) (Op' : List (Ty S.Base) → Ty S.Base → Type) : Type :=
  ∀ {as : List (Ty S.Base)} {τ : Ty S.Base}, S.Op as τ → Op' as τ

namespace Term

variable {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Translate the operators of a term. -/
def mapOp (f : OpMap S Op') : {Γ : Ctx S.Base} → {τ : Ty S.Base} →
    Term S Γ τ → Term (S.withOps Op') Γ τ
  | _, _, var x => var x
  | _, _, lam t => lam (mapOp f t)
  | _, _, app t u => app (mapOp f t) (mapOp f u)
  | _, _, op o args => op (f o) fun _ x => mapOp f (args _ x)

/-- Translating operators along a map that is injective on operators is
injective on terms. -/
theorem mapOp_injective {f : OpMap S Op'} (hf : ∀ {as τ}, Function.Injective (@f as τ)) :
    Function.Injective (mapOp f : Term S Γ τ → Term (S.withOps Op') Γ τ) := sorry

end Term

namespace Term

variable {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Interpret the operators of a term as derived operators over the same base
types. -/
def translate
    (F : ∀ {as : List (Ty S.Base)} {τ : Ty S.Base}, S.Op as τ → Term (S.withOps Op') as τ) :
    {Γ : Ctx S.Base} → {τ : Ty S.Base} → Term S Γ τ → Term (S.withOps Op') Γ τ
  | _, _, var x => var x
  | _, _, lam t => lam (translate F t)
  | _, _, app t u => app (translate F t) (translate F u)
  | _, _, op o args => (F o).subst fun _ x => translate F (args _ x)

variable {F : ∀ {as : List (Ty S.Base)} {τ : Ty S.Base}, S.Op as τ → Term (S.withOps Op') as τ}

/-- An operator translation is the derived-operator translation sending each
operator to itself applied to its argument variables. -/
theorem mapOp_eq_translate (f : OpMap S Op') (t : Term S Γ τ) :
    t.mapOp f =
      t.translate (fun o => .op (S := S.withOps Op') (f o) fun _ x => .var x) := sorry

end Term

end Silmaril.Calculi.TypedLambda
