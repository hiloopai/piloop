module

public import Silmaril.Semantics.Domain.Function
public import Silmaril.Calculi.TypedLambda.Context

/-! # Denotational semantics in domains

§6 of the notes, for any typed λ-calculus over a signature. A `Model` assigns a
domain to each base type and a continuous function to each operator. Then:

* types denote domains: `⟦σ -> τ⟧` is the domain of continuous functions
  `⟦σ⟧ →𝒄 ⟦τ⟧` (Definition 22);
* a context `Γ` denotes the product of the domains of its variables, an
  environment (Definition 23, Remark 9);
* a term `Γ ⊢ t : τ` denotes a continuous function `⟦Γ⟧ →𝒄 ⟦τ⟧`
  (Definitions 25–26). Variables are projections, application is evaluation
  and abstraction is currying.

The denotation is defined by structural recursion into continuous maps, so it
is well defined and continuous by construction (Theorem 25). It is
compositional (Theorem 26, through the denotation of contexts of Remark 13)
and commutes with substitution (Proposition 27).
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open OmegaCompletePartialOrder Silmaril.Semantics.Domain Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {S : Signature}

/-- Definition 22: the domain denoted by a type, given domains for base types. -/
@[reducible] def Ty.denote {B : Type} (base : B → DomainCat.{0}) : Ty B → DomainCat.{0}
  | .base b => base b
  | .arrow σ τ => DomainCat.of (Ty.denote base σ →𝒄 Ty.denote base τ)

/-- Environments over given base domains: one value for each variable. -/
abbrev TyEnv {B : Type} (base : B → DomainCat.{0}) (Γ : List (Ty B)) : Type :=
  Tuple (fun a => (Ty.denote base a : Type)) Γ

theorem ωScottContinuous_tyEnv {B : Type} {base : B → DomainCat.{0}} {D : Type*}
    [OmegaCompletePartialOrder D] {Γ : List (Ty B)} {f : D → TyEnv base Γ}
    (h : ∀ a x, ωScottContinuous fun d => f d a x) : ωScottContinuous f :=
  ωScottContinuous.of_apply₂ (β := fun a => Var Γ a → (Ty.denote base a : Type)) fun a =>
    ωScottContinuous.of_apply₂ (β := fun _ => (Ty.denote base a : Type)) fun x => h a x

/-- The projection of an environment onto a variable. An environment is a
nested product (Remark 9), so this is Proposition 12's projection `evalHom`
applied twice: first to the type, then to the variable. -/
def projHom {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)} {a : Ty B} (x : Var Γ a) :
    TyEnv base Γ →𝒄 Ty.denote base a :=
  (evalHom (P := fun _ : Var Γ a => (Ty.denote base a : Type)) x).comp
    (evalHom (P := fun a => Var Γ a → (Ty.denote base a : Type)) a)

@[simp] theorem projHom_apply {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)}
    {a : Ty B} (x : Var Γ a) (ρ : TyEnv base Γ) : projHom base x ρ = ρ a x := rfl

/-- `projHom` is a composite of Proposition 12's projections. -/
theorem projHom_eq_evalHom_comp {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)}
    {a : Ty B} (x : Var Γ a) :
    projHom base x = (evalHom (P := fun _ : Var Γ a => (Ty.denote base a : Type)) x).comp
      (evalHom (P := fun a => Var Γ a → (Ty.denote base a : Type)) a) := rfl

/-- A model of a signature in domains. -/
structure Model (S : Signature) where
  /-- The domain denoted by each base type. -/
  base : S.Base → DomainCat.{0}
  /-- The continuous function denoted by each operator. -/
  op : {as : List (Ty S.Base)} → {τ : Ty S.Base} → S.Op as τ →
    (TyEnv base as →𝒄 Ty.denote base τ)

namespace Model

variable (M : Model S)

/-- The domain `⟦τ⟧`. -/
abbrev ty (τ : Ty S.Base) : Type := Ty.denote M.base τ

/-- Definition 23: environments `⟦Γ⟧`, one value for each variable. -/
abbrev Env (Γ : Ctx S.Base) : Type := TyEnv M.base Γ

variable {M}

/-- View an element of `⟦σ -> τ⟧` as a continuous function. -/
abbrev fn {σ τ : Ty S.Base} (f : M.ty (σ ⤳ τ)) : M.ty σ →𝒄 M.ty τ := f

/-- An element of `⟦σ -> τ⟧` from a continuous function. -/
abbrev ofFn {σ τ : Ty S.Base} (f : M.ty σ →𝒄 M.ty τ) : M.ty (σ ⤳ τ) := f

/-- Semantic application: `f(d)` for `f ∈ ⟦σ -> τ⟧`. -/
abbrev app {σ τ : Ty S.Base} (f : M.ty (σ ⤳ τ)) (d : M.ty σ) : M.ty τ := fn f d

/-- Semantic application to two arguments: `f(d)(e)` for
`f ∈ ⟦σ -> σ' -> τ⟧`. -/
abbrev app₂ {σ σ' τ : Ty S.Base} (f : M.ty (σ ⤳ σ' ⤳ τ)) (d : M.ty σ) (e : M.ty σ') :
    M.ty τ :=
  app (app f d) e

/-- Extensionality in `⟦σ -> τ⟧`: elements with the same applications are
equal. -/
theorem ext_app {σ τ : Ty S.Base} {f g : M.ty (σ ⤳ τ)} (h : ∀ d, app f d = app g d) :
    f = g :=
  ContinuousHom.ext (fn f) (fn g) h

/-- Extensionality in `⟦σ -> σ' -> τ⟧` for two arguments. -/
theorem ext_app₂ {σ σ' τ : Ty S.Base} {f g : M.ty (σ ⤳ σ' ⤳ τ)}
    (h : ∀ d e, app₂ f d e = app₂ g d e) : f = g :=
  ext_app fun d => ext_app (h d)

theorem ωScottContinuous_env {D : Type*} [OmegaCompletePartialOrder D] {Γ : Ctx S.Base}
    {f : D → M.Env Γ} (h : ∀ a x, ωScottContinuous fun d => f d a x) : ωScottContinuous f :=
  ωScottContinuous_tyEnv h

theorem ωScottContinuous_env_apply {Γ : Ctx S.Base} {a : Ty S.Base} (x : Var Γ a) :
    ωScottContinuous fun ρ : M.Env Γ => ρ a x :=
  (projHom M.base x).ωScottContinuous

/-- Continuity of semantic application. -/
theorem ωScottContinuous_app {D : Type*} [OmegaCompletePartialOrder D] {σ τ : Ty S.Base}
    {f : D → M.ty (σ ⤳ τ)} {g : D → M.ty σ} (hf : ωScottContinuous f) (hg : ωScottContinuous g) :
    ωScottContinuous fun d => app (f d) (g d) :=
  ContinuousHom.ωScottContinuous_apply (f := fun d => fn (f d)) hf hg

/-- The environment `ρ[x ↦ d]` extending `ρ` with a value for a new variable. -/
def consHom {Γ : Ctx S.Base} {σ : Ty S.Base} : M.Env Γ × M.ty σ →𝒄 M.Env (σ :: Γ) :=
  ContinuousHom.ofFun (fun p => p.2 ::ₜ p.1) <| ωScottContinuous_env fun a x => by
    cases x with
    | zero => exact Prod.ωScottContinuous_snd
    | succ x => exact (ωScottContinuous_env_apply x).comp Prod.ωScottContinuous_fst

@[simp] theorem consHom_apply {Γ : Ctx S.Base} {σ : Ty S.Base} (ρ : M.Env Γ) (d : M.ty σ) :
    consHom (ρ, d) = d ::ₜ ρ := rfl

/-- Remark 9: `⟦Γ, x:σ⟧ ≅ ⟦σ⟧ × ⟦Γ⟧` as orders, splitting an environment into the
value of the newest variable and the rest. -/
def envConsIso {Γ : Ctx S.Base} {σ : Ty S.Base} : M.Env (σ :: Γ) ≃o M.ty σ × M.Env Γ where
  toFun ρ := (ρ.head, ρ.tail)
  invFun p := p.1 ::ₜ p.2
  left_inv ρ := (Tuple.eq_cons ρ).symm
  right_inv _ := rfl
  map_rel_iff' {ρ ρ'} := by
    change (ρ.head, ρ.tail) ≤ (ρ'.head, ρ'.tail) ↔ ρ ≤ ρ'
    rw [Prod.mk_le_mk]
    constructor
    · rintro ⟨h₁, h₂⟩ a x
      cases x with
      | zero => exact h₁
      | succ x => exact h₂ a x
    · intro h
      exact ⟨h _ .zero, fun a x => h a (.succ x)⟩

/-- The order on environments of an extended context is componentwise. -/
theorem env_cons_le_iff {Γ : Ctx S.Base} {σ : Ty S.Base} {ρ ρ' : M.Env (σ :: Γ)} :
    ρ ≤ ρ' ↔ ρ.head ≤ ρ'.head ∧ ρ.tail ≤ ρ'.tail :=
  (envConsIso.le_iff_le (x := ρ) (y := ρ')).symm.trans Prod.mk_le_mk

/-- Tuple a family of continuous maps into an environment. An environment is a
nested product (Remark 9), so this is Proposition 12's tupling `piHom` applied
twice. -/
def tupleHom {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) : D →𝒄 M.Env as :=
  piHom (P := fun a => Var as a → M.ty a) fun a => piHom (P := fun _ : Var as a => M.ty a) (f a)

@[simp] theorem tupleHom_apply {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) (d : D) : tupleHom f d = fun a x => f a x d := rfl

/-- `tupleHom` is a nesting of Proposition 12's tuplings. -/
theorem tupleHom_eq_piHom {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) :
    tupleHom f =
      piHom (P := fun a => Var as a → M.ty a) fun a => piHom (P := fun _ : Var as a => M.ty a)
        (f a) := rfl

/-- Tupling is monotone in the family of component maps. -/
theorem tupleHom_mono {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    {f g : ∀ a, Var as a → (D →𝒄 M.ty a)} (h : ∀ a x, f a x ≤ g a x) :
    tupleHom f ≤ tupleHom g := fun d a x => h a x d

/-- The projection of an environment onto a variable. -/
abbrev varHom {Γ : Ctx S.Base} {τ : Ty S.Base} (x : Var Γ τ) : M.Env Γ →𝒄 M.ty τ :=
  projHom M.base x

variable (M)

/-- Definitions 24–26: the denotation `⟦Γ ⊢ t : τ⟧ : ⟦Γ⟧ →𝒄 ⟦τ⟧`. -/
def denote : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Term S Γ τ → (M.Env Γ →𝒄 M.ty τ)
  | _, _, .var x => varHom x
  | _, _, .lam t => curry ((denote t).comp consHom)
  | _, _, .app t u => ContinuousHom.ofFun (fun ρ => app (denote t ρ) (denote u ρ))
      (ωScottContinuous_app (denote t).ωScottContinuous (denote u).ωScottContinuous)
  | _, _, .op o args => (M.op o).comp (tupleHom fun a x => denote (args a x))

variable {M}

variable {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

@[simp] theorem denote_var (x : Var Γ τ) (ρ : M.Env Γ) : M.denote (.var x) ρ = ρ τ x := rfl

@[simp] theorem denote_lam (t : Term S (σ :: Γ) τ) (ρ : M.Env Γ) (d : M.ty σ) :
    app (M.denote (.lam t) ρ) d = M.denote t (d ::ₜ ρ) := rfl

@[simp] theorem denote_app (t : Term S Γ (σ ⤳ τ)) (u : Term S Γ σ) (ρ : M.Env Γ) :
    M.denote (.app t u) ρ = app (M.denote t ρ) (M.denote u ρ) := rfl

theorem denote_lam_ext {Γ' : Ctx S.Base} {t : Term S (σ :: Γ) τ} {t' : Term S (σ :: Γ') τ}
    {ρ : M.Env Γ} {ρ' : M.Env Γ'}
    (h : ∀ d : M.ty σ, M.denote t (d ::ₜ ρ) = M.denote t' (d ::ₜ ρ')) :
    M.denote (.lam t) ρ = M.denote (.lam t') ρ' :=
  ext_app h

@[simp] theorem denote_op {as : List (Ty S.Base)} (o : S.Op as τ) (args : Tuple (Term S Γ) as)
    (ρ : M.Env Γ) : M.denote (.op o args) ρ = M.op o (fun a x => M.denote (args a x) ρ) := rfl

/-- Remark 12: the denotation of a closed term, an element of `⟦τ⟧`. -/
abbrev denoteClosed (t : Term S [] τ) : M.ty τ := M.denote t Tuple.nil

/-- The empty environment is the only one (Remark 9). -/
theorem env_nil_eq (ρ : M.Env []) : ρ = Tuple.nil := Tuple.eq_nil ρ

/-- Renaming commutes with denotation. -/
theorem denote_rename (ρ : Ren Γ Δ) (t : Term S Γ τ) (η : M.Env Δ) :
    M.denote (t.rename ρ) η = M.denote t (fun a x => η a (ρ a x)) := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih =>
      simp only [Term.rename]
      apply denote_lam_ext
      intro d
      rw [ih]
      congr 1
      exact Tuple.ext fun _ x => by cases x <;> rfl
  | app t u iht ihu => simp only [Term.rename, denote_app, iht, ihu]
  | op o args ih => simp only [Term.rename, denote_op, ih]

theorem denote_weaken (t : Term S Γ τ) (d : M.ty σ) (η : M.Env Γ) :
    M.denote t.weaken (d ::ₜ η) = M.denote t η :=
  denote_rename _ t _

/-- A closed term, weakened into any context, denotes the same element in every
environment. -/
theorem denote_ofClosed (t : Term S [] τ) (ρ : M.Env Γ) :
    M.denote (Term.ofClosed t) ρ = M.denoteClosed t := by
  rw [Term.ofClosed, denote_rename, env_nil_eq (fun a x => ρ a _)]

/-- Proposition 27, for parallel substitution: `⟦t[s]⟧(η) = ⟦t⟧(⟦s⟧(η))`. -/
theorem denote_subst (s : Subst S Γ Δ) (t : Term S Γ τ) (η : M.Env Δ) :
    M.denote (t.subst s) η = M.denote t (fun a x => M.denote (s a x) η) := by
  induction t generalizing Δ with
  | var => rfl
  | lam t ih =>
      simp only [Term.subst]
      apply denote_lam_ext
      intro d
      rw [ih]
      congr 1
      refine Tuple.ext fun _ x => ?_
      cases x with
      | zero => rfl
      | succ x => exact denote_weaken _ _ _
  | app t u iht ihu => simp only [Term.subst, denote_app, iht, ihu]
  | op o args ih => simp only [Term.subst, denote_op, ih]

/-- Proposition 27: `⟦t[u/x]⟧(ρ) = ⟦t⟧(ρ[x ↦ ⟦u⟧(ρ)])`. -/
theorem denote_subst1 (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) (ρ : M.Env Γ) :
    M.denote (t.subst1 u) ρ = M.denote t (M.denote u ρ ::ₜ ρ) := by
  rw [Term.subst1, denote_subst]
  congr 1
  exact Tuple.ext fun _ x => by cases x <;> rfl

/-- Proposition 27 for closed terms: `⟦t[u/x]⟧ = ⟦t⟧(⟦u⟧)`. -/
theorem denoteClosed_subst1 (t : Term S [σ] τ) (u : Term S [] σ) :
    M.denoteClosed (t.subst1 u) = app (M.denote (.lam t) Tuple.nil) (M.denoteClosed u) := by
  show M.denote (t.subst1 u) Tuple.nil = _
  rw [denote_subst1, denote_lam]

end Model

namespace Context

open Model

/-- Remark 13: the denotation of a context, a function from denotations of the
hole to denotations of the whole. -/
def denote {Δ : Ctx S.Base} {σ : Ty S.Base} (M : Model S) :
    {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ →
    (M.Env Δ →𝒄 M.ty σ) → (M.Env Γ →𝒄 M.ty τ)
  | _, _, .hole, f => f
  | _, _, .lam C, f => curry ((denote M C f).comp consHom)
  | _, _, .appL C u, f => ContinuousHom.ofFun (fun ρ => app (denote M C f ρ) (M.denote u ρ))
      (ωScottContinuous_app (denote M C f).ωScottContinuous (M.denote u).ωScottContinuous)
  | _, _, .appR g C, f => ContinuousHom.ofFun (fun ρ => app (M.denote g ρ) (denote M C f ρ))
      (ωScottContinuous_app (M.denote g).ωScottContinuous (denote M C f).ωScottContinuous)
  | _, _, .op o args x C, f => (M.op o).comp <| tupleHom <|
      Tuple.set (X := fun a => M.Env _ →𝒄 M.ty a) (fun a y => M.denote (args a y)) x
        (denote M C f)

/-- Remark 13: `⟦C[t]⟧ = ⟦C⟧(⟦t⟧)`. -/
theorem denote_plug {M : Model S} {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}
    (C : Context S Δ σ Γ τ) (t : Term S Δ σ) :
    M.denote (C.plug t) = denote M C (M.denote t) := by
  induction C with
  | hole => rfl
  | lam C ih =>
      simp only [plug, Model.denote, denote, ih]
  | appL C u ih =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [plug, Model.denote_app, ih]
      rfl
  | appR g C ih =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [plug, Model.denote_app, ih]
      rfl
  | op o args x C ih =>
      simp only [plug, Model.denote, denote]
      congr 2
      rw [← ih]
      exact Tuple.map_set (fun _ t => M.denote t) args x (C.plug t)

/-- The denotation of a context is monotone in the denotation of the hole. -/
theorem denote_mono {M : Model S} {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}
    (C : Context S Δ σ Γ τ) {f g : M.Env Δ →𝒄 M.ty σ} (h : f ≤ g) :
    denote M C f ≤ denote M C g := by
  induction C with
  | hole => exact h
  | lam C ih => exact fun ρ d => ih (d ::ₜ ρ)
  | appL C u ih => exact fun ρ => ih ρ (M.denote u ρ)
  | appR t C ih => exact fun ρ => (fn (M.denote t ρ)).monotone (ih ρ)
  | op o args x C ih =>
      exact fun ρ => (M.op o).monotone fun a y =>
        Tuple.set_mono (X := fun a => M.Env _ →𝒄 M.ty a)
          (fun a y => M.denote (args a y)) x ih a y ρ

end Context


namespace Model

variable {M : Model S} {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Theorem 26 (compositionality): terms with equal denotations have equal
denotations in every context. -/
theorem denote_plug_eq {t u : Term S Δ σ} (h : M.denote t = M.denote u)
    (C : Context S Δ σ Γ τ) : M.denote (C.plug t) = M.denote (C.plug u) := by
  rw [Context.denote_plug, Context.denote_plug, h]

/-- Compositionality for the order: if `⟦t⟧ ⊑ ⟦u⟧` then `⟦C[t]⟧ ⊑ ⟦C[u]⟧`. -/
theorem denote_plug_le {t u : Term S Δ σ} (h : M.denote t ≤ M.denote u)
    (C : Context S Δ σ Γ τ) : M.denote (C.plug t) ≤ M.denote (C.plug u) := by
  rw [Context.denote_plug, Context.denote_plug]
  exact Context.denote_mono C h

/-- Closed terms have equal denotations iff they denote the same element
(Remark 12): the empty environment is the only one. -/
theorem denote_eq_iff_denoteClosed_eq {t u : Term S [] τ} :
    M.denote t = M.denote u ↔ M.denoteClosed t = M.denoteClosed u :=
  ⟨fun h => by rw [denoteClosed, h],
    fun h => ContinuousHom.ext _ _ fun ρ => by rw [env_nil_eq ρ]; exact h⟩

/-- Closed terms have ordered denotations iff their elements are ordered. -/
theorem denote_le_iff_denoteClosed_le {t u : Term S [] τ} :
    M.denote t ≤ M.denote u ↔ M.denoteClosed t ≤ M.denoteClosed u :=
  ⟨fun h => h Tuple.nil, fun h ρ => by rw [env_nil_eq ρ]; exact h⟩

section Morphism

variable {Op' : List (Ty S.Base) → Ty S.Base → Type}

/-- Interpret the operators `Op'` over the base domains of `M`. -/
abbrev withOps (M : Model S)
    (op' : {as : List (Ty S.Base)} → {τ : Ty S.Base} → Op' as τ →
      (TyEnv M.base as →𝒄 Ty.denote M.base τ)) :
    Model (S.withOps Op') :=
  ⟨M.base, op'⟩

variable {op' : {as : List (Ty S.Base)} → {τ : Ty S.Base} → Op' as τ →
  (TyEnv M.base as →𝒄 Ty.denote M.base τ)}

/-- An operator translation that preserves the interpretation of operators
preserves denotations. -/
theorem denote_mapOp {f : OpMap S Op'} (h : ∀ {as τ} (o : S.Op as τ), op' (f o) = M.op o)
    (t : Term S Γ τ) : (M.withOps op').denote (t.mapOp f) = M.denote t := by
  induction t with
  | var => rfl
  | lam t ih =>
      refine ContinuousHom.ext _ _ fun ρ => ext_app fun d => ?_
      change (M.withOps op').denote (t.mapOp f) (d ::ₜ ρ) = M.denote t (d ::ₜ ρ)
      rw [ih]
  | app t u iht ihu =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [Term.mapOp, denote_app]
      rw [iht, ihu]
  | op o args ih =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [Term.mapOp, denote_op, h, ih]

/-- A derived-operator translation that interprets each operator by a term
with the same denotation preserves denotations. -/
theorem denote_translate
    {F : ∀ {as : List (Ty S.Base)} {τ : Ty S.Base}, S.Op as τ → Term (S.withOps Op') as τ}
    (h : ∀ {as τ} (o : S.Op as τ), (M.withOps op').denote (F o) = M.op o)
    (t : Term S Γ τ) : (M.withOps op').denote (t.translate F) = M.denote t := by
  induction t with
  | var => rfl
  | lam t ih =>
      refine ContinuousHom.ext _ _ fun ρ => ext_app fun d => ?_
      change (M.withOps op').denote (t.translate F) (d ::ₜ ρ) = M.denote t (d ::ₜ ρ)
      rw [ih]
  | app t u iht ihu =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [Term.translate, denote_app]
      rw [iht, ihu]
  | op o args ih =>
      refine ContinuousHom.ext _ _ fun ρ => ?_
      simp only [Term.translate, denote_op]
      rw [denote_subst, h]
      simp only [ih]

end Morphism

end Model

namespace Observation

/-! ### Theorem 24, abstractly

The notes prove Theorem 24 (denotational equality implies contextual
equivalence) from three properties of PCF's model: soundness (Theorem 28),
compositionality (Theorem 26) and adequacy (Theorem 31). Compositionality holds
in every model, so the argument works for any observation whose evaluation is
sound and adequate at ground types. -/

variable {O : Observation S} {M : Model S} {Γ : Ctx S.Base} {τ : Ty S.Base}

/-- Theorem 24 for the contextual preorder, in any model: if evaluation at
ground types is sound (a result has the denotation of its program) and adequate
(a program whose denotation is above that of a result evaluates to it), then
`⟦t⟧ ⊑ ⟦u⟧` implies `t ≤ctx u`. -/
theorem ctxLe_of_denote_le
    (sound : ∀ {γ}, O.Ground γ → ∀ {t v : Term S [] γ}, O.Eval t v →
      M.denoteClosed t = M.denoteClosed v)
    (adequate : ∀ {γ}, O.Ground γ → ∀ {t t' v : Term S [] γ}, O.Eval t' v →
      M.denoteClosed v ≤ M.denoteClosed t → O.Eval t v)
    {t u : Term S Γ τ} (h : M.denote t ≤ M.denote u) : O.CtxLe t u := by
  intro γ hγ C v hv
  refine adequate hγ hv ?_
  rw [← sound hγ hv]
  exact Model.denote_plug_le h C Tuple.nil

/-- Theorem 24 (denotational equality implies contextual equivalence), in any
model in which evaluation at ground types is sound and adequate. -/
theorem ctxEquiv_of_denote_eq
    (sound : ∀ {γ}, O.Ground γ → ∀ {t v : Term S [] γ}, O.Eval t v →
      M.denoteClosed t = M.denoteClosed v)
    (adequate : ∀ {γ}, O.Ground γ → ∀ {t t' v : Term S [] γ}, O.Eval t' v →
      M.denoteClosed v ≤ M.denoteClosed t → O.Eval t v)
    {t u : Term S Γ τ} (h : M.denote t = M.denote u) : O.CtxEquiv t u :=
  ctxEquiv_iff_le.2
    ⟨ctxLe_of_denote_le sound adequate h.le, ctxLe_of_denote_le sound adequate h.ge⟩

variable (O M) in
/-- The hypotheses of Theorem 24: evaluation at ground types is sound (a result
has the denotation of its program) and adequate (a program whose denotation is
above that of a result evaluates to it) for the model `M`. -/
structure SoundAdequate : Prop where
  /-- Soundness at ground types. -/
  sound : ∀ {γ}, O.Ground γ → ∀ {t v : Term S [] γ}, O.Eval t v →
    M.denoteClosed t = M.denoteClosed v
  /-- Adequacy at ground types. -/
  adequate : ∀ {γ}, O.Ground γ → ∀ {t t' v : Term S [] γ}, O.Eval t' v →
    M.denoteClosed v ≤ M.denoteClosed t → O.Eval t v

/-- Theorem 24 for the contextual preorder, from `SoundAdequate`. -/
theorem SoundAdequate.ctxLe_of_denote_le (hM : O.SoundAdequate M) {t u : Term S Γ τ}
    (h : M.denote t ≤ M.denote u) : O.CtxLe t u :=
  Observation.ctxLe_of_denote_le hM.sound hM.adequate h

/-- Theorem 24, from `SoundAdequate`. -/
theorem SoundAdequate.ctxEquiv_of_denote_eq (hM : O.SoundAdequate M) {t u : Term S Γ τ}
    (h : M.denote t = M.denote u) : O.CtxEquiv t u :=
  Observation.ctxEquiv_of_denote_eq hM.sound hM.adequate h

end Observation

end Silmaril.Calculi.TypedLambda
