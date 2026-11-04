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

theorem mapOp_rename (f : OpMap S Op') (ρ : Ren Γ Δ) (t : Term S Γ τ) :
    (t.rename ρ).mapOp f = (t.mapOp f).rename (S := S.withOps Op') ρ := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih => simp only [rename, mapOp, ih]; rfl
  | app t u iht ihu => simp only [rename, mapOp, iht, ihu]
  | op o args ih => simp only [rename, mapOp, ih]

theorem mapOp_subst (f : OpMap S Op') (s : Subst S Γ Δ) (t : Term S Γ τ) :
    (t.subst s).mapOp f =
      (t.mapOp f).subst (S := S.withOps Op') (fun _ x => (s _ x).mapOp f) := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih =>
      simp only [subst, mapOp, ih]
      congr 2
      refine Tuple.ext fun _ x => ?_
      cases x with
      | zero => rfl
      | succ x => exact mapOp_rename f _ _
  | app t u iht ihu => simp only [subst, mapOp, iht, ihu]
  | op o args ih => simp only [subst, mapOp, ih]

theorem mapOp_subst1 (f : OpMap S Op') (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) :
    (t.subst1 u).mapOp f = (t.mapOp f).subst1 (S := S.withOps Op') (u.mapOp f) := by
  rw [subst1, mapOp_subst, subst1]
  congr 1
  exact Tuple.ext fun _ x => by cases x <;> rfl

/-- Translating operators along a map that is injective on operators is
injective on terms. -/
theorem mapOp_injective {f : OpMap S Op'} (hf : ∀ {as τ}, Function.Injective (@f as τ)) :
    Function.Injective (mapOp f : Term S Γ τ → Term (S.withOps Op') Γ τ) := by
  intro t t' h
  induction t with
  | var x =>
      cases t' with
      | var y =>
          simp only [mapOp, var.injEq] at h
          rw [h]
      | _ => cases h
  | lam t ih =>
      cases t' with
      | lam t' =>
          simp only [mapOp, lam.injEq] at h
          rw [ih h]
      | _ => cases h
  | app t u iht ihu =>
      cases t' with
      | app t' u' =>
          simp only [mapOp] at h
          obtain ⟨rfl, h₁, h₂⟩ := app.inj h
          rw [iht (eq_of_heq h₁), ihu (eq_of_heq h₂)]
      | _ => cases h
  | op o args ih =>
      cases t' with
      | op o' args' =>
          simp only [mapOp] at h
          obtain ⟨rfl, ho, hargs⟩ := op.inj h
          cases hf (eq_of_heq ho)
          refine congrArg (op o) (funext fun a => funext fun x => ih a x ?_)
          exact congrFun (congrFun (eq_of_heq hargs) a) x
      | _ => cases h

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

theorem translate_rename (ρ : Ren Γ Δ) (t : Term S Γ τ) :
    (t.rename ρ).translate F = (t.translate F).rename (S := S.withOps Op') ρ := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih => simp only [rename, translate, ih]; rfl
  | app t u iht ihu => simp only [rename, translate, iht, ihu]
  | op o args ih =>
      simp only [rename, translate, ih]
      rw [rename_subst (S := S.withOps Op')]

theorem translate_subst (s : Subst S Γ Δ) (t : Term S Γ τ) :
    (t.subst s).translate F =
      (t.translate F).subst (S := S.withOps Op') (fun _ x => (s _ x).translate F) := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih =>
      simp only [subst, translate, ih]
      congr 2
      refine Tuple.ext fun _ x => ?_
      cases x with
      | zero => rfl
      | succ x => exact translate_rename _ _
  | app t u iht ihu => simp only [subst, translate, iht, ihu]
  | op o args ih =>
      simp only [subst, translate, ih]
      rw [subst_subst (S := S.withOps Op')]
      rfl

theorem translate_subst1 (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) :
    (t.subst1 u).translate F = (t.translate F).subst1 (S := S.withOps Op') (u.translate F) := by
  rw [subst1, translate_subst, subst1]
  congr 1
  exact Tuple.ext fun _ x => by cases x <;> rfl

/-- An operator translation is the derived-operator translation sending each
operator to itself applied to its argument variables. -/
theorem mapOp_eq_translate (f : OpMap S Op') (t : Term S Γ τ) :
    t.mapOp f =
      t.translate (fun o => .op (S := S.withOps Op') (f o) fun _ x => .var x) := by
  induction t with
  | var => rfl
  | lam t ih => simp only [mapOp, translate, ih]
  | app t u iht ihu => simp only [mapOp, translate, iht, ihu]
  | op o args ih => simp only [mapOp, translate, ih]; rfl

end Term

end Silmaril.Calculi.TypedLambda
