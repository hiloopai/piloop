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
    (h : ∀ a x, ωScottContinuous fun d => f d a x) : ωScottContinuous f := sorry

/-- The projection of an environment onto a variable. An environment is a
nested product (Remark 9), so this is Proposition 12's projection `evalHom`
applied twice: first to the type, then to the variable. -/
def projHom {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)} {a : Ty B} (x : Var Γ a) :
    TyEnv base Γ →𝒄 Ty.denote base a :=
  (evalHom (P := fun _ : Var Γ a => (Ty.denote base a : Type)) x).comp
    (evalHom (P := fun a => Var Γ a → (Ty.denote base a : Type)) a)

@[simp] theorem projHom_apply {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)}
    {a : Ty B} (x : Var Γ a) (ρ : TyEnv base Γ) : projHom base x ρ = ρ a x := sorry

/-- `projHom` is a composite of Proposition 12's projections. -/
theorem projHom_eq_evalHom_comp {B : Type} (base : B → DomainCat.{0}) {Γ : List (Ty B)}
    {a : Ty B} (x : Var Γ a) :
    projHom base x = (evalHom (P := fun _ : Var Γ a => (Ty.denote base a : Type)) x).comp
      (evalHom (P := fun a => Var Γ a → (Ty.denote base a : Type)) a) := sorry

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
    f = g := sorry

theorem ωScottContinuous_env {D : Type*} [OmegaCompletePartialOrder D] {Γ : Ctx S.Base}
    {f : D → M.Env Γ} (h : ∀ a x, ωScottContinuous fun d => f d a x) : ωScottContinuous f := sorry

theorem ωScottContinuous_env_apply {Γ : Ctx S.Base} {a : Ty S.Base} (x : Var Γ a) :
    ωScottContinuous fun ρ : M.Env Γ => ρ a x := sorry

/-- Continuity of semantic application. -/
theorem ωScottContinuous_app {D : Type*} [OmegaCompletePartialOrder D] {σ τ : Ty S.Base}
    {f : D → M.ty (σ ⤳ τ)} {g : D → M.ty σ} (hf : ωScottContinuous f) (hg : ωScottContinuous g) :
    ωScottContinuous fun d => app (f d) (g d) := sorry

/-- The environment `ρ[x ↦ d]` extending `ρ` with a value for a new variable. -/
def consHom {Γ : Ctx S.Base} {σ : Ty S.Base} : M.Env Γ × M.ty σ →𝒄 M.Env (σ :: Γ) :=
  ContinuousHom.ofFun (fun p => p.2 ::ₜ p.1) <| ωScottContinuous_env fun a x => by
    cases x with
    | zero => exact Prod.ωScottContinuous_snd
    | succ x => exact (ωScottContinuous_env_apply x).comp Prod.ωScottContinuous_fst

@[simp] theorem consHom_apply {Γ : Ctx S.Base} {σ : Ty S.Base} (ρ : M.Env Γ) (d : M.ty σ) :
    consHom (ρ, d) = d ::ₜ ρ := sorry

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
    ρ ≤ ρ' ↔ ρ.head ≤ ρ'.head ∧ ρ.tail ≤ ρ'.tail := sorry

/-- Tuple a family of continuous maps into an environment. An environment is a
nested product (Remark 9), so this is Proposition 12's tupling `piHom` applied
twice. -/
def tupleHom {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) : D →𝒄 M.Env as :=
  piHom (P := fun a => Var as a → M.ty a) fun a => piHom (P := fun _ : Var as a => M.ty a) (f a)

@[simp] theorem tupleHom_apply {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) (d : D) : tupleHom f d = fun a x => f a x d := sorry

/-- `tupleHom` is a nesting of Proposition 12's tuplings. -/
theorem tupleHom_eq_piHom {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    (f : ∀ a, Var as a → (D →𝒄 M.ty a)) :
    tupleHom f =
      piHom (P := fun a => Var as a → M.ty a) fun a => piHom (P := fun _ : Var as a => M.ty a)
        (f a) := sorry

/-- Tupling is monotone in the family of component maps. -/
theorem tupleHom_mono {D : Type} [OmegaCompletePartialOrder D] {as : List (Ty S.Base)}
    {f g : ∀ a, Var as a → (D →𝒄 M.ty a)} (h : ∀ a x, f a x ≤ g a x) :
    tupleHom f ≤ tupleHom g := sorry

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

theorem denote_lam_ext {Γ' : Ctx S.Base} {t : Term S (σ :: Γ) τ} {t' : Term S (σ :: Γ') τ}
    {ρ : M.Env Γ} {ρ' : M.Env Γ'}
    (h : ∀ d : M.ty σ, M.denote t (d ::ₜ ρ) = M.denote t' (d ::ₜ ρ')) :
    M.denote (.lam t) ρ = M.denote (.lam t') ρ' := sorry

@[simp] theorem denote_op {as : List (Ty S.Base)} (o : S.Op as τ) (args : Tuple (Term S Γ) as)
    (ρ : M.Env Γ) : M.denote (.op o args) ρ = M.op o (fun a x => M.denote (args a x) ρ) := sorry

/-- Remark 12: the denotation of a closed term, an element of `⟦τ⟧`. -/
abbrev denoteClosed (t : Term S [] τ) : M.ty τ := M.denote t Tuple.nil

/-- The empty environment is the only one (Remark 9). -/
theorem env_nil_eq (ρ : M.Env []) : ρ = Tuple.nil := sorry

/-- Renaming commutes with denotation. -/
theorem denote_rename (ρ : Ren Γ Δ) (t : Term S Γ τ) (η : M.Env Δ) :
    M.denote (t.rename ρ) η = M.denote t (fun a x => η a (ρ a x)) := sorry

/-- A closed term, weakened into any context, denotes the same element in every
environment. -/
theorem denote_ofClosed (t : Term S [] τ) (ρ : M.Env Γ) :
    M.denote (Term.ofClosed t) ρ = M.denoteClosed t := sorry

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

end Context


namespace Model

variable {M : Model S} {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Closed terms have ordered denotations iff their elements are ordered. -/
theorem denote_le_iff_denoteClosed_le {t u : Term S [] τ} :
    M.denote t ≤ M.denote u ↔ M.denoteClosed t ≤ M.denoteClosed u := sorry

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
    (h : M.denote t ≤ M.denote u) : O.CtxLe t u := sorry

end Observation

end Silmaril.Calculi.TypedLambda
