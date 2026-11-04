module

public import Silmaril.Calculi.TypedLambda.Syntax

/-! # Renaming and substitution

Renamings `Ren Γ Δ` and parallel substitutions `Subst Γ Δ` are tuples of
variables and terms. Substitution is capture-avoiding by construction, and
it preserves types by construction: Proposition 22 of the notes (typing is
stable under substitution) and Remark 15 (so is parallel substitution) are
the types of `Term.subst1` and `Term.subst`. A parallel closed substitution
`⊢ σ : Γ` (Definition 28) is a `Subst Γ []`.

The usual σ-calculus laws are proved: identity and composition laws for
renaming and substitution, and their fusion.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda

variable {S : Signature}

/-- A renaming from `Γ` to `Δ`. -/
abbrev Ren (Γ Δ : Ctx S.Base) : Type := Tuple (Var Δ) Γ

/-- A parallel substitution replacing the variables of `Γ` by terms in `Δ`. -/
abbrev Subst (S : Signature) (Γ Δ : Ctx S.Base) : Type := Tuple (Term S Δ) Γ

namespace Ren

variable {Γ Δ Θ : Ctx S.Base} {σ : Ty S.Base}

/-- The identity renaming. -/
def id (Γ : Ctx S.Base) : Ren Γ Γ := fun _ x => x

/-- Composition of renamings: first `ρ`, then `ρ'`. -/
def comp (ρ' : Ren Δ Θ) (ρ : Ren Γ Δ) : Ren Γ Θ := fun _ x => ρ' _ (ρ _ x)

/-- Weakening by one variable. -/
def weak : Ren Γ (σ :: Γ) := fun _ x => .succ x

/-- Lift a renaming under a binder. -/
def lift (ρ : Ren Γ Δ) : Ren (σ :: Γ) (σ :: Δ) := .zero ::ₜ fun _ x => .succ (ρ _ x)

@[simp] theorem lift_zero (ρ : Ren Γ Δ) : lift (σ := σ) ρ _ .zero = .zero := rfl

@[simp] theorem lift_succ (ρ : Ren Γ Δ) {a : Ty S.Base} (x : Var Γ a) :
    lift (σ := σ) ρ _ (.succ x) = .succ (ρ _ x) := rfl

@[simp] theorem lift_id : lift (σ := σ) (id Γ) = id (σ :: Γ) :=
  Tuple.ext fun _ x => by cases x <;> rfl

theorem lift_comp (ρ' : Ren Δ Θ) (ρ : Ren Γ Δ) :
    lift (σ := σ) (comp ρ' ρ) = comp (lift ρ') (lift ρ) :=
  Tuple.ext fun _ x => by cases x <;> rfl

end Ren

namespace Term

variable {Γ Δ Θ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Rename the free variables of a term. -/
def rename : {Γ Δ : Ctx S.Base} → {τ : Ty S.Base} → Ren Γ Δ → Term S Γ τ → Term S Δ τ
  | _, _, _, ρ, var x => var (ρ _ x)
  | _, _, _, ρ, lam t => lam (rename (Ren.lift ρ) t)
  | _, _, _, ρ, app t u => app (rename ρ t) (rename ρ u)
  | _, _, _, ρ, op o args => op o fun _ x => rename ρ (args _ x)

/-- Weaken a term by one variable. -/
abbrev weaken (t : Term S Γ τ) : Term S (σ :: Γ) τ := t.rename Ren.weak

theorem rename_id (t : Term S Γ τ) : t.rename (Ren.id Γ) = t := by
  induction t with
  | var => rfl
  | lam t ih => simp only [rename, Ren.lift_id, ih]
  | app t u iht ihu => simp only [rename, iht, ihu]
  | op o args ih => simp only [rename, ih]

theorem rename_rename (ρ : Ren Γ Δ) (ρ' : Ren Δ Θ) (t : Term S Γ τ) :
    (t.rename ρ).rename ρ' = t.rename (Ren.comp ρ' ρ) := by
  induction t generalizing Δ Θ with
  | var => rfl
  | lam t ih => simp only [rename, ih, Ren.lift_comp]
  | app t u iht ihu => simp only [rename, iht, ihu]
  | op o args ih => simp only [rename, ih]

end Term

namespace Subst

variable {Γ Δ Θ : Ctx S.Base} {σ τ : Ty S.Base}

/-- The identity substitution. -/
def id (Γ : Ctx S.Base) : Subst S Γ Γ := fun _ x => .var x

/-- Lift a substitution under a binder. -/
def lift (s : Subst S Γ Δ) : Subst S (σ :: Γ) (σ :: Δ) :=
  .var .zero ::ₜ fun _ x => (s _ x).weaken

/-- Precompose a substitution with a renaming. -/
def ofRen (ρ : Ren Γ Δ) : Subst S Γ Δ := fun _ x => .var (ρ _ x)

/-- Substitute a single term for the innermost variable, `t[u/x]`. -/
def single (u : Term S Γ σ) : Subst S (σ :: Γ) Γ := u ::ₜ id Γ

@[simp] theorem lift_zero (s : Subst S Γ Δ) : lift (σ := σ) s _ .zero = .var .zero := rfl

@[simp] theorem lift_succ (s : Subst S Γ Δ) {a : Ty S.Base} (x : Var Γ a) :
    lift (σ := σ) s _ (.succ x) = (s _ x).weaken := rfl

theorem lift_id : lift (σ := σ) (id (S := S) Γ) = id (σ :: Γ) :=
  Tuple.ext fun _ x => by cases x <;> rfl

theorem lift_ofRen (ρ : Ren Γ Δ) : lift (σ := σ) (ofRen (S := S) ρ) = ofRen (Ren.lift ρ) :=
  Tuple.ext fun _ x => by cases x <;> rfl

end Subst

namespace Term

variable {Γ Δ Θ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Apply a parallel substitution. -/
def subst : {Γ Δ : Ctx S.Base} → {τ : Ty S.Base} → Subst S Γ Δ → Term S Γ τ → Term S Δ τ
  | _, _, _, s, var x => s _ x
  | _, _, _, s, lam t => lam (subst (Subst.lift s) t)
  | _, _, _, s, app t u => app (subst s t) (subst s u)
  | _, _, _, s, op o args => op o fun _ x => subst s (args _ x)

/-- Proposition 22: single substitution `t[u/x]`. -/
abbrev subst1 (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) : Term S Γ τ := t.subst (Subst.single u)

/-- Substitution leaves constants (nullary operators) unchanged. -/
@[simp] theorem subst_op_nil {o : S.Op [] τ} (s : Subst S Γ Δ) :
    (op o Tuple.nil : Term S Γ τ).subst s = op o Tuple.nil :=
  congrArg (op o) (Tuple.ext fun _ x => x.elim)

/-- Renaming leaves constants (nullary operators) unchanged. -/
@[simp] theorem rename_op_nil {o : S.Op [] τ} (ρ : Ren Γ Δ) :
    (op o Tuple.nil : Term S Γ τ).rename ρ = op o Tuple.nil :=
  congrArg (op o) (Tuple.ext fun _ x => x.elim)

theorem subst_id (t : Term S Γ τ) : t.subst (Subst.id Γ) = t := by
  induction t with
  | var => rfl
  | lam t ih => simp only [subst, Subst.lift_id, ih]
  | app t u iht ihu => simp only [subst, iht, ihu]
  | op o args ih => simp only [subst, ih]

theorem subst_ofRen (ρ : Ren Γ Δ) (t : Term S Γ τ) : t.subst (Subst.ofRen ρ) = t.rename ρ := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih => simp only [subst, rename, Subst.lift_ofRen, ih]
  | app t u iht ihu => simp only [subst, rename, iht, ihu]
  | op o args ih => simp only [subst, rename, ih]

/-- Substitution after renaming. -/
theorem subst_rename (ρ : Ren Γ Δ) (s : Subst S Δ Θ) (t : Term S Γ τ) :
    (t.rename ρ).subst s = t.subst (fun _ x => s _ (ρ _ x)) := by
  induction t generalizing Δ Θ with
  | var => rfl
  | lam t ih =>
      simp only [rename, subst, ih]
      congr 2
      exact Tuple.ext fun _ x => by cases x <;> rfl
  | app t u iht ihu => simp only [rename, subst, iht, ihu]
  | op o args ih => simp only [rename, subst, ih]

/-- Renaming after substitution. -/
theorem rename_subst (s : Subst S Γ Δ) (ρ : Ren Δ Θ) (t : Term S Γ τ) :
    (t.subst s).rename ρ = t.subst (fun _ x => (s _ x).rename ρ) := by
  induction t generalizing Δ Θ with
  | var => rfl
  | lam t ih =>
      simp only [rename, subst, ih]
      congr 2
      refine Tuple.ext fun _ x => ?_
      cases x with
      | zero => rfl
      | succ x =>
          change ((s _ x).rename Ren.weak).rename (Ren.lift ρ) = ((s _ x).rename ρ).rename Ren.weak
          rw [rename_rename, rename_rename]
          rfl
  | app t u iht ihu => simp only [rename, subst, iht, ihu]
  | op o args ih => simp only [rename, subst, ih]

/-- Composition of substitutions. -/
def _root_.Silmaril.Calculi.TypedLambda.Subst.comp (s' : Subst S Δ Θ) (s : Subst S Γ Δ) : Subst S Γ Θ :=
  fun _ x => (s _ x).subst s'

theorem lift_comp (s' : Subst S Δ Θ) (s : Subst S Γ Δ) :
    Subst.lift (σ := σ) (Subst.comp s' s) = Subst.comp (Subst.lift s') (Subst.lift s) := by
  refine Tuple.ext fun _ x => ?_
  cases x with
  | zero => rfl
  | succ x =>
      change ((s _ x).subst s').rename Ren.weak = ((s _ x).rename Ren.weak).subst (Subst.lift s')
      rw [rename_subst, subst_rename]
      rfl

/-- Substitution after substitution. -/
theorem subst_subst (s : Subst S Γ Δ) (s' : Subst S Δ Θ) (t : Term S Γ τ) :
    (t.subst s).subst s' = t.subst (Subst.comp s' s) := by
  induction t generalizing Δ Θ with
  | var => rfl
  | lam t ih => simp only [subst, ih, lift_comp]
  | app t u iht ihu => simp only [subst, iht, ihu]
  | op o args ih => simp only [subst, ih]

/-- Substitution into a closed term is the identity. -/
@[simp] theorem subst_nil (s : Subst S [] []) (t : Term S [] τ) : t.subst s = t := by
  rw [show s = Subst.id [] from (Tuple.eq_nil s).trans (Tuple.eq_nil _).symm, subst_id]

/-- Substituting into a weakened term discards the new variable. -/
@[simp] theorem weaken_subst1 (t : Term S Γ τ) (u : Term S Γ σ) : t.weaken.subst1 u = t := by
  rw [subst1, subst_rename]
  exact subst_id t

/-- The substitution lemma for a lifted substitution followed by a single one:
`t[σ⁺][u/x] = t[σ, x ↦ u]`. -/
theorem subst_lift_subst1 (s : Subst S Γ Δ) (t : Term S (σ :: Γ) τ) (u : Term S Δ σ) :
    (t.subst (Subst.lift s)).subst1 u = t.subst (u ::ₜ s) := by
  rw [subst1, subst_subst]
  congr 1
  refine Tuple.ext fun _ x => ?_
  cases x with
  | zero => rfl
  | succ x => exact weaken_subst1 _ _

/-! ### Closed terms -/

/-- Weaken a closed term into an arbitrary context. -/
def ofClosed (t : Term S [] τ) : Term S Γ τ := t.rename fun _ x => x.elim

/-- A closed term, weakened into the empty context, is itself. -/
@[simp] theorem ofClosed_nil (t : Term S [] τ) : (ofClosed t : Term S [] τ) = t := by
  rw [ofClosed, show (fun _ x => x.elim : Ren [] []) = Ren.id [] from
    Tuple.ext fun _ x => x.elim]
  exact rename_id t

/-- Substitution fixes closed terms. -/
@[simp] theorem ofClosed_subst (t : Term S [] τ) (s : Subst S Γ Δ) :
    (ofClosed t).subst s = ofClosed t := by
  rw [ofClosed, ofClosed, subst_rename, ← subst_ofRen]
  exact congrArg (t.subst ·) (Tuple.ext fun _ x => x.elim)

/-- Renaming fixes closed terms. -/
@[simp] theorem ofClosed_rename (t : Term S [] τ) (ρ : Ren Γ Δ) :
    (ofClosed t).rename ρ = ofClosed t := by
  rw [ofClosed, rename_rename]
  exact congrArg (t.rename ·) (Tuple.ext fun _ x => x.elim)

@[simp] theorem subst_var (s : Subst S Γ Δ) (x : Var Γ τ) : (var x).subst s = s _ x := rfl

@[simp] theorem subst_lam (s : Subst S Γ Δ) (t : Term S (σ :: Γ) τ) :
    (lam t).subst s = lam (t.subst (Subst.lift s)) := rfl

@[simp] theorem subst_app (s : Subst S Γ Δ) (t : Term S Γ (.arrow σ τ)) (u : Term S Γ σ) :
    (app t u).subst s = app (t.subst s) (u.subst s) := rfl

@[simp] theorem rename_var (ρ : Ren Γ Δ) (x : Var Γ τ) : (var x).rename ρ = var (ρ _ x) := rfl

@[simp] theorem rename_lam (ρ : Ren Γ Δ) (t : Term S (σ :: Γ) τ) :
    (lam t).rename ρ = lam (t.rename (Ren.lift ρ)) := rfl

@[simp] theorem rename_app (ρ : Ren Γ Δ) (t : Term S Γ (.arrow σ τ)) (u : Term S Γ σ) :
    (app t u).rename ρ = app (t.rename ρ) (u.rename ρ) := rfl

/-- Renaming a closed term weakens it. -/
@[simp] theorem rename_closed (t : Term S [] τ) (ρ : Ren [] Δ) : t.rename ρ = ofClosed t :=
  congrArg (t.rename ·) (Tuple.ext fun _ x => x.elim)

/-- Substituting into a closed term weakens it. -/
@[simp] theorem subst_closed (t : Term S [] τ) (s : Subst S [] Δ) : t.subst s = ofClosed t := by
  rw [ofClosed, ← subst_ofRen]
  exact congrArg (t.subst ·) (Tuple.ext fun _ x => x.elim)

/-- Substituting into a weakened term under a lifted substitution. -/
@[simp] theorem weaken_subst_lift (t : Term S Γ τ) (s : Subst S Γ Δ) :
    (t.weaken (σ := σ)).subst (Subst.lift s) = (t.subst s).weaken := by
  rw [subst_rename, weaken, rename_subst]
  rfl

/-- Substituting into a weakened term discards the first component. -/
@[simp] theorem weaken_subst_cons (t : Term S Γ τ) (u : Term S Δ σ) (s : Subst S Γ Δ) :
    t.weaken.subst (u ::ₜ s) = t.subst s := by
  rw [subst_rename]
  rfl

end Term

end Silmaril.Calculi.TypedLambda
