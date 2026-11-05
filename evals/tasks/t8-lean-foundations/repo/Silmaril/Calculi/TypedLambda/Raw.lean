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
theorem unique (h : HasType Γ t τ) (h' : HasType Γ t τ') : τ = τ' := sorry

/-- Inversion for variables. -/
theorem var_inv {n : ℕ} (h : HasType Γ (Raw.var n) τ) : Γ[n]? = some τ := sorry

/-- Inversion for abstractions at a function type. -/
theorem lam_inv {ρ : Ty S.Base} (h : HasType Γ (Raw.lam σ t) (σ' ⤳ ρ)) :
    σ = σ' ∧ HasType (σ :: Γ) t ρ := sorry

/-- An abstraction has no base type. -/
theorem not_lam_base {b : S.Base} : ¬HasType Γ (Raw.lam σ t) (.base b) := sorry

/-- Inversion for operators. -/
theorem op_inv {as : List (Ty S.Base)} {o : S.Op as τ} {args : Fin as.length → Raw S}
    (h : HasType Γ (Raw.op o args) τ') : τ = τ' ∧ ∀ i, HasType Γ (args i) (as.get i) := sorry

end HasType

/-- Church's fixed point combinator `fun f:τ₁. (fun x:τ₂. f (x x)) (fun x:τ₂. f (x x))`,
with type annotations on its binders. -/
def churchY (τ₁ τ₂ : Ty S.Base) : Raw S :=
  lam τ₁ (app (lam τ₂ (app (var 1) (app (var 0) (var 0))))
    (lam τ₂ (app (var 1) (app (var 0) (var 0)))))

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
    infer Γ t = some τ ↔ HasType Γ t τ := sorry

/-- Typing is decidable, by type inference. -/
instance {Γ : Ctx S.Base} {t : Raw S} {τ : Ty S.Base} : Decidable (HasType Γ t τ) :=
  decidable_of_iff _ infer_eq_some

/-- A well-typed term has an inferred type. -/
theorem HasType.infer_ne_none {Γ : Ctx S.Base} {t : Raw S} {τ : Ty S.Base}
    (h : HasType Γ t τ) : infer Γ t ≠ none := sorry

/-- The argument of a well-typed application has an inferred type. -/
theorem HasType.infer_arg_ne_none {Γ : Ctx S.Base} {t u : Raw S} {τ : Ty S.Base}
    (h : HasType Γ (Raw.app t u) τ) : infer Γ u ≠ none := sorry

/-- Inversion for applications, given the inferred argument type. -/
theorem HasType.app_inv {Γ : Ctx S.Base} {t u : Raw S} {σ τ : Ty S.Base}
    (h : HasType Γ (Raw.app t u) τ) (hu : infer Γ u = some σ) :
    HasType Γ t (σ ⤳ τ) ∧ HasType Γ u σ := sorry

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
theorem erase_hasType (t : Term S Γ τ) : Raw.HasType Γ t.erase τ := sorry

/-- Erasure is injective: an intrinsic term is determined by its raw term. -/
theorem erase_injective : Function.Injective (erase : Term S Γ τ → Raw S) := sorry

/-- Transport a term along an equality of types. -/
def castTy (h : τ = τ') (t : Term S Γ τ) : Term S Γ τ' := h ▸ t

/-- Erasure forgets type casts. -/
@[simp] theorem erase_castTy (h : τ = τ') (t : Term S Γ τ) : (t.castTy h).erase = t.erase := sorry

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
    (t.elab h).erase = t := sorry

/-- Erasure followed by elaboration is the identity. -/
@[simp] theorem elab_erase {Γ : Ctx S.Base} {τ : Ty S.Base} (t : Term S Γ τ)
    (h : HasType Γ t.erase τ := t.erase_hasType) : t.erase.elab h = t := sorry

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

theorem lam_subst : (lam σ t)[k := u] = lam σ (t[k + 1 := u.shift]) := sorry

theorem app_subst : (app t v)[k := u] = app (t[k := u]) (v[k := u]) := sorry

theorem op_subst {as : List (Ty S.Base)} (o : S.Op as τ) (args : Fin as.length → Raw S) :
    (op o args)[k := u] = op o fun i => (args i)[k := u] := sorry

namespace HasType

variable {Γ Δ : Ctx S.Base}

/-- Shifting a closed term does nothing. -/
theorem shift_eq (h : HasType [] t τ) : t.shift = t := sorry

end HasType

end Raw

/-! ### Intrinsic renamings as index renamings -/

namespace Ren

variable {Γ Δ Θ : Ctx S.Base} {σ : Ty S.Base}

/-- The intrinsic renaming `ρ` acts on de Bruijn indices as `f`. -/
def Tracks (ρ : Ren Γ Δ) (f : ℕ → ℕ) : Prop := ∀ a (x : Var Γ a), (ρ a x).toNat = f x.toNat

/-- The identity renaming acts on indices as the identity. -/
theorem Tracks.id : (Ren.id Γ).Tracks _root_.id := sorry

/-- Weakening acts on indices as the successor. -/
theorem Tracks.weak : (Ren.weak (σ := σ) : Ren Γ _).Tracks Nat.succ := sorry

/-- Composition of renamings acts on indices as composition. -/
theorem Tracks.comp {ρ' : Ren Δ Θ} {ρ : Ren Γ Δ} {g f : ℕ → ℕ} (h' : ρ'.Tracks g)
    (h : ρ.Tracks f) : (Ren.comp ρ' ρ).Tracks (g ∘ f) := sorry

/-- An intrinsic renaming is the in-scope restriction of a typed index renaming
(`DeBruijn.TypedRen`, Isotope's `SSA.Ren`): any function on indices that it
tracks sends typed indices of `Γ` to indices of `Δ` of the same type. -/
theorem Tracks.typedRen {ρ : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) :
    DeBruijn.TypedRen Γ Δ f := sorry

/-- The intrinsic renaming determined by a typed index renaming: each position `x` of `Γ`
goes to the position of `Δ` with index `f x.toNat`. -/
def ofTypedRen {f : ℕ → ℕ} (hf : DeBruijn.TypedRen Γ Δ f) : Ren Γ Δ :=
  fun _ x => Var.ofGetElem? (f x.toNat) (hf (Var.get_toNat x))

/-- Every intrinsic renaming is `ofTypedRen` of any index function it tracks: intrinsic
renamings are exactly typed index renamings restricted to in-scope indices. -/
theorem Tracks.eq_ofTypedRen {ρ : Ren Γ Δ} {f : ℕ → ℕ} (h : ρ.Tracks f) :
    ρ = Ren.ofTypedRen h.typedRen := sorry

end Ren

namespace Term

variable {Γ Δ : Ctx S.Base} {σ τ : Ty S.Base}

/-- Erasure commutes with single substitution:
`(t[u/x]).erase = t.erase[0 := u.erase]`. -/
theorem erase_subst1 (t : Term S (σ :: Γ) τ) (u : Term S Γ σ) :
    (t.subst1 u).erase = t.erase[0 := u.erase] := sorry

end Term

end Silmaril.Calculi.TypedLambda
