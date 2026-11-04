module

public import Silmaril.Calculi.TypedLambda.Substitution
public import Silmaril.Prelude.DeBruijnTyped

/-! # Raw terms and extrinsic typing

The notes present PCF (§5.1) with *raw* terms (Fig. 1) and a separate typing
relation `Γ ⊢ t : τ` (Fig. 2), and set `Pcf_{Γ,τ} = {t ∣ Γ ⊢ t : τ}`
(Definition 17). This file gives that presentation for an arbitrary
signature and proves it equivalent to the intrinsically typed `Term`.

* `Raw S` are untyped de Bruijn terms whose abstractions carry the type of
  the bound variable, as in the notes' `fun x:τ. t`.
* `Raw.HasType Γ t τ` is the generic form of Fig. 2: the rules for
  variables, abstraction and application, and one rule per operator.
* Typing is unique (`Raw.HasType.unique`, Remark 10) and decidable, by a
  type inference function `Raw.infer` that is sound and complete.
* Erasure `Term.erase` forgets the typing of an intrinsic term, and
  elaboration `Raw.elab` rebuilds it from a typing proof. Together they give
  `Raw.equivTerm : {t // HasType Γ t τ} ≃ Term S Γ τ`, the formal content of
  Definition 17. Since `HasType` is a proposition, a well-typed raw term has
  exactly one intrinsic counterpart, which justifies Remark 10's conflation
  of terms with their typing derivations.
* Raw renaming, shifting and substitution `t[k := u]` (via CSLib's
  `HasSubstitution`) preserve typing (Proposition 22), and erasure commutes
  with them.
* Self-application `x x` has no type, so neither has Church's fixed point
  combinator (`not_hasType_churchY`, Exercise 12).

Substitution `t[k := u]` replaces index `k` by `u` and decrements the indices
above `k`. The term `u` lives in the context of the result, so it is shifted
when going under a binder.

Source: the lecture notes, §5.1 (printed pages 38–40,
PDF pages 39–41), §5.4, Exercise 12 (printed page 46, PDF page 47) and §6.2.2,
Remark 10 (printed page 49, PDF page 50). The syntax is generalized to an arbitrary
operator signature; the PCF-specific evaluation rules are not defined here.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda

open Cslib.LambdaCalculus.LocallyNameless.Stlc Cslib

variable {S : Signature}

/-- Figure 1: raw terms over the signature `S`, with de Bruijn indices for
variables and a type annotation on each abstraction. -/
inductive Raw (S : Signature) : Type
  /-- The variable with de Bruijn index `n`. -/
  | var (n : ℕ) : Raw S
  /-- Function abstraction `fun x : σ. t`. -/
  | lam (σ : Ty S.Base) (t : Raw S) : Raw S
  /-- Application `t u`. -/
  | app (t u : Raw S) : Raw S
  /-- A fully applied operator. -/
  | op {as : List (Ty S.Base)} {τ : Ty S.Base} (o : S.Op as τ) (args : Fin as.length → Raw S) :
      Raw S

namespace Raw

/-- Figure 2: the typing relation `Γ ⊢ t : τ`, generalized to any signature. -/
inductive HasType : Ctx S.Base → Raw S → Ty S.Base → Prop
  /-- `Γ(x) = τ` gives `Γ ⊢ x : τ`. -/
  | var {Γ : Ctx S.Base} {n : ℕ} {τ : Ty S.Base} : Γ[n]? = some τ → HasType Γ (Raw.var n) τ
  /-- `Γ, x : σ ⊢ t : τ` gives `Γ ⊢ fun x : σ. t : σ -> τ`. -/
  | lam {Γ : Ctx S.Base} {σ τ : Ty S.Base} {t : Raw S} :
      HasType (σ :: Γ) t τ → HasType Γ (Raw.lam σ t) (σ ⤳ τ)
  /-- `Γ ⊢ t : σ -> τ` and `Γ ⊢ u : σ` give `Γ ⊢ t u : τ`. -/
  | app {Γ : Ctx S.Base} {σ τ : Ty S.Base} {t u : Raw S} :
      HasType Γ t (σ ⤳ τ) → HasType Γ u σ → HasType Γ (Raw.app t u) τ
  /-- An operator applied to arguments of its argument types. -/
  | op {Γ : Ctx S.Base} {as : List (Ty S.Base)} {τ : Ty S.Base} (o : S.Op as τ)
      {args : Fin as.length → Raw S} :
      (∀ i, HasType Γ (args i) (as.get i)) → HasType Γ (Raw.op o args) τ

namespace HasType

variable {Γ : Ctx S.Base} {t u : Raw S} {σ σ' τ τ' : Ty S.Base}

/-- Remark 10: a raw term has at most one type in a given context. -/
theorem unique (h : HasType Γ t τ) (h' : HasType Γ t τ') : τ = τ' := by
  induction h generalizing τ' with
  | var hn => cases h' with
    | var hn' => exact Option.some.inj (hn.symm.trans hn')
  | lam _ ih => cases h' with
    | lam h' => rw [ih h']
  | app _ _ iht _ => cases h' with
    | app ht' _ => exact (Ty.arrow.inj (iht ht')).2
  | op => cases h' with
    | op => rfl

/-- Inversion for variables. -/
theorem var_inv {n : ℕ} (h : HasType Γ (Raw.var n) τ) : Γ[n]? = some τ := by
  cases h with
  | var hn => exact hn

/-- Inversion for abstractions at a function type. -/
theorem lam_inv {ρ : Ty S.Base} (h : HasType Γ (Raw.lam σ t) (σ' ⤳ ρ)) :
    σ = σ' ∧ HasType (σ :: Γ) t ρ := by
  cases h with
  | lam h => exact ⟨rfl, h⟩

/-- An abstraction has no base type. -/
theorem not_lam_base {b : S.Base} : ¬HasType Γ (Raw.lam σ t) (.base b) := nofun

/-- Inversion for operators. -/
theorem op_inv {as : List (Ty S.Base)} {o : S.Op as τ} {args : Fin as.length → Raw S}
    (h : HasType Γ (Raw.op o args) τ') : τ = τ' ∧ ∀ i, HasType Γ (args i) (as.get i) := by
  cases h with
  | op _ h => exact ⟨rfl, h⟩

/-- Self-application `x x` of a variable has no type. -/
theorem not_app_var_self {n : ℕ} : ¬HasType Γ (Raw.app (Raw.var n) (Raw.var n)) τ := by
  intro h
  cases h with
  | app ht hu => exact Ty.arrow_ne_self _ _ (Option.some.inj ((var_inv ht).symm.trans (var_inv hu)))

end HasType

/-- Church's fixed point combinator `fun f:τ₁. (fun x:τ₂. f (x x)) (fun x:τ₂. f (x x))`,
with type annotations on its binders. -/
def churchY (τ₁ τ₂ : Ty S.Base) : Raw S :=
  lam τ₁ (app (lam τ₂ (app (var 1) (app (var 0) (var 0))))
    (lam τ₂ (app (var 1) (app (var 0) (var 0)))))

/-- Exercise 12: Church's fixed point combinator has no simple type, whatever the
annotations. The self-application `x x` cannot be typed. -/
theorem not_hasType_churchY (Γ : Ctx S.Base) (τ₁ τ₂ τ₃ : Ty S.Base) :
    ¬HasType Γ (churchY τ₁ τ₂) τ₃ := by
  intro h
  cases h with
  | lam h =>
    cases h with
    | app hf _ =>
      cases hf with
      | lam hf =>
        cases hf with
        | app _ hx => exact HasType.not_app_var_self hx

section Infer

variable [DecidableEq S.Base]

/-- Type inference: the unique type of a raw term, if it has one. -/
def infer : Ctx S.Base → Raw S → Option (Ty S.Base)
  | Γ, var n => Γ[n]?
  | Γ, lam σ t => (infer (σ :: Γ) t).map (σ ⤳ ·)
  | Γ, app t u =>
      match infer Γ t, infer Γ u with
      | some (.arrow σ τ), some σ' => if σ = σ' then some τ else none
      | _, _ => none
  | Γ, op (as := as) (τ := τ) _ args =>
      if ∀ i, infer Γ (args i) = some (as.get i) then some τ else none

/-- Type inference is sound and complete for `HasType`. -/
theorem infer_eq_some {Γ : Ctx S.Base} {t : Raw S} {τ : Ty S.Base} :
    infer Γ t = some τ ↔ HasType Γ t τ := by
  constructor
  · intro h
    induction t generalizing Γ τ with
    | var n => exact .var h
    | lam σ t ih =>
        simp only [infer, Option.map_eq_some_iff] at h
        obtain ⟨ρ, h, rfl⟩ := h
        exact .lam (ih h)
    | app t u iht ihu =>
        simp only [infer] at h
        rcases ht : infer Γ t with _ | (_ | ⟨σ, ρ⟩) <;>
          rcases hu : infer Γ u with _ | σ' <;> simp only [ht, hu, reduceCtorEq] at h
        split_ifs at h with hσ
        cases h
        cases hσ
        exact .app (iht ht) (ihu hu)
    | op o args ih =>
        simp only [infer] at h
        split_ifs at h with hargs
        cases h
        exact .op o fun i => ih i (hargs i)
  · intro h
    induction h with
    | var hn => exact hn
    | lam _ ih => simp [infer, ih]
    | app _ _ iht ihu => simp [infer, iht, ihu]
    | op o _ ih => simp [infer, ih]

/-- Typing is decidable, by type inference. -/
instance {Γ : Ctx S.Base} {t : Raw S} {τ : Ty S.Base} : Decidable (HasType Γ t τ) :=
  decidable_of_iff _ infer_eq_some

/-- A well-typed term has an inferred type. -/
theorem HasType.infer_ne_none {Γ : Ctx S.Base} {t : Raw S} {τ : Ty S.Base}
    (h : HasType Γ t τ) : infer Γ t ≠ none := by
  rw [infer_eq_some.mpr h]
  exact Option.some_ne_none τ

/-- The argument of a well-typed application has an inferred type. -/
theorem HasType.infer_arg_ne_none {Γ : Ctx S.Base} {t u : Raw S} {τ : Ty S.Base}
    (h : HasType Γ (Raw.app t u) τ) : infer Γ u ≠ none := by
  cases h with
  | app _ hu => exact hu.infer_ne_none

/-- Inversion for applications, given the inferred argument type. -/
theorem HasType.app_inv {Γ : Ctx S.Base} {t u : Raw S} {σ τ : Ty S.Base}
    (h : HasType Γ (Raw.app t u) τ) (hu : infer Γ u = some σ) :
    HasType Γ t (σ ⤳ τ) ∧ HasType Γ u σ := by
  cases h with
  | app ht hu' =>
      cases (infer_eq_some.mp hu).unique hu'
      exact ⟨ht, hu'⟩

end Infer

end Raw

namespace Term

variable {Γ Δ : Ctx S.Base} {σ τ τ' : Ty S.Base}

/-- Erase the typing of an intrinsic term, leaving a raw term. -/
def erase : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Term S Γ τ → Raw S
  | _, _, var x => .var x.toNat
  | _, _, lam (σ := σ) t => .lam σ t.erase
  | _, _, app t u => .app t.erase u.erase
  | _, _, op o args => .op o fun i => (args _ (Var.ofFin i)).erase

/-- An intrinsic term erases to a raw term of the same type. -/
theorem erase_hasType (t : Term S Γ τ) : Raw.HasType Γ t.erase τ := by
  induction t with
  | var x => exact .var x.get_toNat
  | lam _ ih => exact .lam ih
  | app _ _ iht ihu => exact .app iht ihu
  | op o _ ih => exact .op o fun i => ih _ _

/-- Erasure is injective: an intrinsic term is determined by its raw term. -/
theorem erase_injective : Function.Injective (erase : Term S Γ τ → Raw S) := by
  intro t t' h
  induction t with
  | var x =>
      cases t' with
      | var y => exact congrArg var (Var.toNat_injective (Raw.var.inj h))
      | _ => simp [erase] at h
  | lam t ih =>
      cases t' with
      | lam t' => exact congrArg lam (ih (Raw.lam.inj h).2)
      | _ => simp [erase] at h
  | app t u iht ihu =>
      cases t' with
      | app t' u' =>
          simp only [erase, Raw.app.injEq] at h
          have hσ := (erase_hasType u).unique (h.2 ▸ erase_hasType u')
          subst hσ
          rw [iht h.1, ihu h.2]
      | _ => simp [erase] at h
  | op o args ih =>
      cases t' with
      | op o' args' =>
          obtain ⟨rfl, -, ho, hargs⟩ := Raw.op.inj h
          cases eq_of_heq ho
          rw [← Tuple.ofFn_eta args, ← Tuple.ofFn_eta args']
          exact congrArg (op o ∘ Tuple.ofFn)
            (funext fun i => ih _ _ (congrFun (eq_of_heq hargs) i))
      | _ => simp [erase] at h

/-- Transport a term along an equality of types. -/
def castTy (h : τ = τ') (t : Term S Γ τ) : Term S Γ τ' := h ▸ t

/-- Erasure forgets type casts. -/
@[simp] theorem erase_castTy (h : τ = τ') (t : Term S Γ τ) : (t.castTy h).erase = t.erase := by
  subst h
  rfl

end Term

namespace Raw

section Elab

variable [DecidableEq S.Base]

/-- Elaborate a well-typed raw term into an intrinsic term. The recursion is
on the raw term, since `HasType` is a proposition. At an application the
argument type is recovered by `infer`. -/
def «elab» : {Γ : Ctx S.Base} → (t : Raw S) → {τ : Ty S.Base} → HasType Γ t τ → Term S Γ τ
  | _, var n, _, h => .var (Var.ofGetElem? n h.var_inv)
  | _, lam _ t, .arrow _ ρ, h =>
      Term.castTy (congrArg (· ⤳ ρ) h.lam_inv.1) (.lam (Raw.elab t h.lam_inv.2))
  | _, lam _ _, .base _, h => absurd h HasType.not_lam_base
  | Γ, app t u, _, h =>
      match hu : infer Γ u with
      | some _ => .app (Raw.elab t (h.app_inv hu).1) (Raw.elab u (h.app_inv hu).2)
      | none => absurd hu h.infer_arg_ne_none
  | _, op o args, _, h =>
      Term.castTy h.op_inv.1 (.op o (Tuple.ofFn fun i => Raw.elab (args i) (h.op_inv.2 i)))

/-- Elaboration followed by erasure is the identity. -/
@[simp] theorem erase_elab {Γ : Ctx S.Base} {τ : Ty S.Base} (t : Raw S) (h : HasType Γ t τ) :
    (t.elab h).erase = t := by
  induction t generalizing Γ τ with
  | var n => simp [Raw.elab, Term.erase]
  | lam σ t ih =>
      cases τ with
      | base b => exact absurd h HasType.not_lam_base
      | arrow σ' ρ => simp [Raw.elab, Term.erase, ih]
  | app t u iht ihu =>
      simp only [Raw.elab]
      split
      · simp [Term.erase, iht, ihu]
      · exact absurd ‹_› h.infer_arg_ne_none
  | op o args ih =>
      simp only [Raw.elab, Term.erase_castTy, Term.erase]
      congr 1
      funext i
      exact (congrArg Term.erase (Tuple.ofFn_ofFin _ i)).trans (ih i _)

/-- Erasure followed by elaboration is the identity. -/
@[simp] theorem elab_erase {Γ : Ctx S.Base} {τ : Ty S.Base} (t : Term S Γ τ)
    (h : HasType Γ t.erase τ := t.erase_hasType) : t.erase.elab h = t :=
  Term.erase_injective (erase_elab _ _)

/-- Definition 17: `Pcf_{Γ,τ} = {t ∣ Γ ⊢ t : τ}`. Well-typed raw terms are
in bijection with intrinsic terms. -/
def equivTerm (Γ : Ctx S.Base) (τ : Ty S.Base) : {t : Raw S // HasType Γ t τ} ≃ Term S Γ τ where
  toFun t := t.1.elab t.2
  invFun t := ⟨t.erase, t.erase_hasType⟩
  left_inv t := Subtype.ext (erase_elab t.1 t.2)
  right_inv t := elab_erase t

end Elab

/-! ### Renaming, shifting and substitution -/

/-- Lift an index renaming under a binder; this is the shared `DeBruijn.lift`. -/
abbrev liftRen (f : ℕ → ℕ) : ℕ → ℕ := DeBruijn.lift f

/-- Rename the free variables of a raw term along a function on indices. -/
def rename (f : ℕ → ℕ) : Raw S → Raw S
  | var n => var (f n)
  | lam σ t => lam σ (rename (DeBruijn.lift f) t)
  | app t u => app (rename f t) (rename f u)
  | op o args => op o fun i => rename f (args i)

/-- Shift all free variables up by one, for weakening by one variable. -/
abbrev shift (t : Raw S) : Raw S := t.rename Nat.succ

/-- Substitute `u` for index `k`, decrementing the indices above `k`. The
term `u` is in the context of the result. -/
def subst : Raw S → ℕ → Raw S → Raw S
  | var n, k, u => if n < k then var n else if n = k then u else var (n - 1)
  | lam σ t, k, u => lam σ (subst t (k + 1) u.shift)
  | app t t', k, u => app (subst t k u) (subst t' k u)
  | op o args, k, u => op o fun i => subst (args i) k u

/-- Raw substitution, written `t[k := u]`. -/
instance : HasSubstitution (Raw S) ℕ (Raw S) where
  subst := subst

variable {k n : ℕ} {σ τ : Ty S.Base} {t u v : Raw S}

theorem var_subst : (var n : Raw S)[k := u] =
    if n < k then var n else if n = k then u else var (n - 1) := rfl

theorem lam_subst : (lam σ t)[k := u] = lam σ (t[k + 1 := u.shift]) := rfl

theorem app_subst : (app t v)[k := u] = app (t[k := u]) (v[k := u]) := rfl

theorem op_subst {as : List (Ty S.Base)} (o : S.Op as τ) (args : Fin as.length → Raw S) :
    (op o args)[k := u] = op o fun i => (args i)[k := u] := rfl

/-- Shifting after substituting for a variable. -/
theorem shift_var_subst : ((var n : Raw S)[k := u]).shift = (var (n + 1))[k + 1 := u.shift] := by
  simp only [var_subst]
  rcases Nat.lt_trichotomy n k with h | rfl | h
  · simp [h, rename]
  · simp
  · have : n ≠ 0 := Nat.ne_zero_of_lt h
    simp only [Nat.not_lt_of_gt h, Nat.ne_of_gt h, ite_false, Nat.add_lt_add_iff_right,
      Nat.add_right_cancel_iff, rename, Nat.add_sub_cancel]
    congr 1
    omega

namespace HasType

variable {Γ Δ : Ctx S.Base}

/-- Renaming preserves typing, provided the renaming is a typed renaming
`DeBruijn.TypedRen Γ Δ f` (the same notion as Isotope's `SSA.Ren`). -/
theorem rename {f : ℕ → ℕ} (hf : DeBruijn.TypedRen Γ Δ f)
    (h : HasType Γ t τ) : HasType Δ (t.rename f) τ := by
  induction h generalizing Δ f with
  | var hn => exact .var (hf hn)
  | lam _ ih => exact .lam (ih (hf.lift _))
  | app _ _ iht ihu => exact .app (iht hf) (ihu hf)
  | op o _ ih => exact .op o fun i => ih i hf

/-- Weakening: shifting a term preserves its type in an extended context. -/
theorem shift (h : HasType Γ t τ) : HasType (σ :: Γ) t.shift τ :=
  h.rename (DeBruijn.TypedRen.wk Γ σ)

/-- Renaming a well-typed term along a map fixing the indices of its context
does nothing. -/
theorem rename_eq {f : ℕ → ℕ} (hf : ∀ n < Γ.length, f n = n) (h : HasType Γ t τ) :
    t.rename f = t := by
  induction h generalizing f with
  | var hn => exact congrArg Raw.var (hf _ (List.getElem?_eq_some_iff.mp hn).1)
  | lam _ ih =>
      refine congrArg _ (ih fun n hn => ?_)
      cases n with
      | zero => rfl
      | succ n => exact congrArg (· + 1) (hf n (Nat.lt_of_succ_lt_succ hn))
  | app _ _ iht ihu => exact congrArg₂ Raw.app (iht hf) (ihu hf)
  | op o _ ih => exact congrArg (Raw.op o) (funext fun i => ih i hf)

/-- Shifting a closed term does nothing. -/
theorem shift_eq (h : HasType [] t τ) : t.shift = t :=
  h.rename_eq fun _ hn => absurd hn (Nat.not_lt_zero _)

/-- Substitution for index `k` preserves typing. -/
theorem subst (h : HasType Γ t τ) (hk : Γ[k]? = some σ) (hu : HasType (Γ.eraseIdx k) u σ) :
    HasType (Γ.eraseIdx k) (t[k := u]) τ := by
  induction h generalizing k u with
  | @var Γ n τ hn =>
      rw [var_subst]
      split_ifs with h₁ h₂
      · exact .var (by rw [List.getElem?_eraseIdx_of_lt h₁]; exact hn)
      · subst h₂
        cases hk.symm.trans hn
        exact hu
      · refine .var ?_
        rw [List.getElem?_eraseIdx_of_ge (by omega), Nat.sub_add_cancel (by omega)]
        exact hn
  | lam _ ih => exact .lam (ih (k := k + 1) hk hu.shift)
  | app _ _ iht ihu => exact .app (iht hk hu) (ihu hk hu)
  | op o _ ih => exact .op o fun i => ih i hk hu

/-- Proposition 22: if `Γ, x : σ ⊢ t : τ` and `Γ ⊢ u : σ` then
`Γ ⊢ t[u/x] : τ`. -/
theorem subst0 (h : HasType (σ :: Γ) t τ) (hu : HasType Γ u σ) : HasType Γ (t[0 := u]) τ :=
  h.subst (k := 0) rfl hu

end HasType

end Raw

/-! ### Intrinsic renamings as index renamings -/

namespace Ren

variable {Γ Δ Θ : Ctx S.Base} {σ : Ty S.Base}

/-- The intrinsic renaming `ρ` acts on de Bruijn indices as `f`. -/
def Tracks (ρ : Ren Γ Δ) (f : ℕ → ℕ) : Prop := ∀ a (x : Var Γ a), (ρ a x).toNat = f x.toNat

/-- The identity renaming acts on indices as the identity. -/
theorem Tracks.id : (Ren.id Γ).Tracks _root_.id := fun _ _ => rfl

/-- Weakening acts on indices as the successor. -/
theorem Tracks.weak : (Ren.weak (σ := σ) : Ren Γ _).Tracks Nat.succ := fun _ _ => rfl

/-- Lifting a renaming under a binder acts on indices as `DeBruijn.lift`: the
typed counterpart of `DeBruijn.lift`. -/
theorem Tracks.lift {ρ : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) :
    (Ren.lift (σ := σ) ρ).Tracks (DeBruijn.lift f) := by
  intro a x
  cases x with
  | zero => rfl
  | succ x => exact congrArg (· + 1) (h _ x)

/-- Composition of renamings acts on indices as composition. -/
theorem Tracks.comp {ρ' : Ren Δ Θ} {ρ : Ren Γ Δ} {g f : ℕ → ℕ} (h' : ρ'.Tracks g)
    (h : ρ.Tracks f) : (Ren.comp ρ' ρ).Tracks (g ∘ f) := fun a x => by
  simp only [Ren.comp, Function.comp_apply, h' a (ρ a x), h a x]

/-- An intrinsic renaming is the in-scope restriction of a typed index renaming
(`DeBruijn.TypedRen`, Isotope's `SSA.Ren`): any function on indices that it
tracks sends typed indices of `Γ` to indices of `Δ` of the same type. -/
theorem Tracks.typedRen {ρ : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) :
    DeBruijn.TypedRen Γ Δ f := by
  intro i A hA
  have hx := h A (Var.ofGetElem? i hA)
  rw [Var.toNat_ofGetElem?] at hx
  rw [← hx]
  exact Var.get_toNat _

/-- The intrinsic renaming determined by a typed index renaming: each position `x` of `Γ`
goes to the position of `Δ` with index `f x.toNat`. -/
def ofTypedRen {f : ℕ → ℕ} (hf : DeBruijn.TypedRen Γ Δ f) : Ren Γ Δ :=
  fun _ x => Var.ofGetElem? (f x.toNat) (hf (Var.get_toNat x))

/-- `ofTypedRen hf` acts on indices as `f`. -/
theorem Tracks.ofTypedRen {f : ℕ → ℕ} (hf : DeBruijn.TypedRen Γ Δ f) :
    (Ren.ofTypedRen hf).Tracks f := fun _ _ => Var.toNat_ofGetElem? _ _

/-- A renaming is determined by the index function it tracks. -/
theorem Tracks.unique {ρ ρ' : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) (h' : ρ'.Tracks f) :
    ρ = ρ' := by
  funext a x
  exact Var.toNat_injective ((h a x).trans (h' a x).symm)

/-- Every intrinsic renaming is `ofTypedRen` of any index function it tracks: intrinsic
renamings are exactly typed index renamings restricted to in-scope indices. -/
theorem Tracks.eq_ofTypedRen {ρ : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) :
    ρ = Ren.ofTypedRen h.typedRen :=
  h.unique (Tracks.ofTypedRen _)

end Ren

namespace Term

variable {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Erasure commutes with renaming, for a renaming acting on indices as `f`. -/
theorem erase_rename (ρ : Ren Γ Δ) (f : ℕ → ℕ)
    (hf : ρ.Tracks f) (t : Term S Γ τ) :
    (t.rename ρ).erase = t.erase.rename f := by
  induction t generalizing Δ f with
  | var x => simp [rename, erase, Raw.rename, hf _ x]
  | lam t ih =>
      simp only [rename, erase, Raw.rename]
      rw [ih _ (DeBruijn.lift f) hf.lift]
  | app t u iht ihu => simp only [rename, erase, Raw.rename, iht _ f hf, ihu _ f hf]
  | op o args ih => simp only [rename, erase, Raw.rename, ih _ _ _ f hf]

/-- Erasure commutes with weakening: a weakened term erases to a shift. -/
theorem erase_weaken (t : Term S Γ τ) : (t.weaken (σ := σ)).erase = t.erase.shift :=
  erase_rename _ _ (fun _ _ => rfl) t

/-- Erasure commutes with substitution, for a parallel substitution that
acts on indices as raw substitution of `v` for index `k`. -/
theorem erase_subst (s : Subst S Γ Δ) (k : ℕ) (v : Raw S)
    (hs : ∀ a (x : Var Γ a), (s a x).erase = (Raw.var x.toNat)[k := v]) (t : Term S Γ τ) :
    (t.subst s).erase = t.erase[k := v] := by
  induction t generalizing Δ k v with
  | var x => exact hs _ x
  | lam t ih =>
      simp only [subst, erase, Raw.lam_subst]
      rw [ih _ (k + 1) v.shift]
      intro a x
      cases x with
      | zero => simp [erase, Raw.var_subst, Var.toNat]
      | succ x =>
          simp only [Subst.lift_succ, erase_weaken, hs, Var.toNat]
          exact Raw.shift_var_subst
  | app t u iht ihu => simp only [subst, erase, Raw.app_subst, iht _ k v hs, ihu _ k v hs]
  | op o args ih => simp only [subst, erase, Raw.op_subst, ih _ _ _ k v hs]

/-- Erasure commutes with single substitution:
`(t[u/x]).erase = t.erase[0 := u.erase]`. -/
theorem erase_subst1 (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) :
    (t.subst1 u).erase = t.erase[0 := u.erase] := by
  refine erase_subst _ 0 u.erase (fun a x => ?_) t
  cases x with
  | zero => rfl
  | succ x => simp [Raw.var_subst, Var.toNat, Subst.single, Subst.id, erase]

end Term

end Silmaril.Calculi.TypedLambda
