module

public import Silmaril.Calculi.TypedLambda.Finite
public import Silmaril.Calculi.PCF.Adequacy

/-! # The boolean fragment PCF_bool

§8.2 of the notes ("Undecidability of contextual equivalence", pp. 68–69).
PCF_bool is the fragment of PCF involving only `bool` and the function types
formed from it, `true`, `false`, conditionals, variables, abstraction,
application, and a primitive divergent term `Ω_bool : bool`.

## Two presentations

The fragment is built in two steps.

* `boolSig` is a signature over the base types of PCF whose operators
  `BoolOp` are `true`, `false`, `if` at PCF_bool types (`Ty.IsBool`) and
  `Ω_bool`. Its inclusion into PCF is a derived-operator translation
  (`TypedLambda.Term.translate`, see `Silmaril.Calculi.TypedLambda.Morphism`): `Ω_bool` becomes
  PCF's `Ω_bool = fix(fun x:bool. x)` and the other operators become
  themselves. The fragment is observed through PCF: a program `t : bool`
  evaluates to `v` when its inclusion does (`boolObs`), and its model
  `boolModel` interprets each operator by the denotation of its translation, so
  the inclusion preserves denotations (`denote_toPCF`). Soundness and adequacy
  transfer along the inclusion (`boolObs_soundAdequate`). Since abstraction is
  generic in `Silmaril.Calculi.TypedLambda.Term`, the terms `BoolTerm` of this signature may
  still bind variables of types mentioning `nat`, and so may its contexts: it
  is a superset of the notes' fragment.
* `pcfBoolSig` is the notes' fragment exactly: its only base type is `Unit`,
  standing for `bool`, so its types (`BoolTy`) are exactly the PCF_bool types
  (`BoolTy.toTy`, `Ty.isBool_iff`), and so are the types of all variables of
  its terms (`PCFBoolTerm`) and of its contexts. `Silmaril.Calculi.TypedLambda.Morphism`
  only translates between signatures with the same base types, so the
  inclusion `PCFBoolTerm.embed` into `boolSig` is defined directly. It
  preserves denotations up to the logical relation `PCFBool.Corr` between the
  two models, which is equality at `bool` (`denoteClosed_embed`). The
  observation `pcfBoolObs` evaluates the inclusion in PCF, and soundness and
  adequacy transfer again (`pcfBoolObs_soundAdequate`), giving Theorem 24 for
  the notes' contextual equivalence of PCF_bool
  (`pcfBool_ctxEquiv_of_denote_eq`).

## Finiteness

The notes' finiteness remarks are then:

* `⟦τ⟧` is finite for every PCF_bool type `τ` (`finite_ty_of_isBool`, and an
  instance under `Fact (Ty.IsBool τ)`; for the exact fragment, the instance
  `pcfBool_finite_ty` at every type): `𝔹⊥` is finite and function domains
  between finite domains are finite (`Silmaril.Calculi.TypedLambda.Ty.finite_denote`).
* On these domains every monotone map is continuous
  (`ωScottContinuous_of_monotone_of_isBool`), so `⟦σ -> τ⟧` is the finite
  poset of monotone "truth tables" (`arrowOrderIsoOfIsBool`).
* There are finitely many PCF_bool terms up to contextual equivalence at each
  type, in each context (`pcfBool_finite_ctxQuot`), and at most as many as
  there are elements of `⟦Γ⟧ →𝒄 ⟦τ⟧` (`pcfBool_card_ctxQuot_le`). This is
  `Silmaril.Calculi.TypedLambda.Observation.SoundAdequate.finite_ctxQuot` for the fragment.
  The same holds for the larger `boolSig`, at PCF_bool types and contexts
  (`finite_boolCtxQuot`, `card_boolCtxQuot_le`).

Loader's theorem, that definability of elements of `⟦τ⟧` by PCF_bool terms and
contextual equivalence of PCF_bool terms are undecidable (uniformly in `τ`),
is cited background and is not formalized.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-! ### PCF_bool types -/

/-- The PCF_bool types: `bool` and the function types formed from it. -/
abbrev Ty.IsBool (τ : Ty) : Prop := TypedLambda.Ty.AllBase (· = Base.bool) τ

/-- `bool` is a PCF_bool type. -/
theorem Ty.isBool_bool : Ty.IsBool Ty.bool := sorry

/-- `nat` is not a PCF_bool type. -/
theorem Ty.not_isBool_nat : ¬ Ty.IsBool Ty.nat := sorry

/-- A function type is a PCF_bool type iff its domain and codomain are. -/
theorem Ty.isBool_arrow {σ τ : Ty} : Ty.IsBool (σ ⤳ τ) ↔ Ty.IsBool σ ∧ Ty.IsBool τ := sorry

instance Ty.fact_isBool_bool : Fact (Ty.IsBool Ty.bool) := ⟨rfl⟩

instance Ty.fact_isBool_arrow {σ τ : Ty} [Fact (Ty.IsBool σ)] [Fact (Ty.IsBool τ)] :
    Fact (Ty.IsBool (σ ⤳ τ)) :=
  ⟨⟨Fact.out, Fact.out⟩⟩

variable {p : Bool}

/-! ### Finiteness of the PCF_bool domains -/

/-- The base domain of the PCF_bool types, `𝔹⊥`, is finite. -/
theorem finite_base_of_isBool : ∀ b : Base, b = Base.bool → Finite (baseDom b) := sorry

/-- `⟦τ⟧` is finite for every PCF_bool type `τ`. -/
theorem finite_ty_of_isBool {τ : Ty} (hτ : Ty.IsBool τ) : Finite ((model p).ty τ) := sorry

/-- `⟦τ⟧` is finite for every PCF_bool type `τ`, as an instance. -/
instance finite_ty {τ : Ty} [h : Fact (Ty.IsBool τ)] : Finite ((model p).ty τ) :=
  finite_ty_of_isBool h.out

/-- `⟦Γ⟧` is finite for every context `Γ` of PCF_bool types. -/
theorem finite_env_of_isBool {Γ : Ctx Base} (hΓ : ∀ a ∈ Γ, Ty.IsBool a) :
    Finite ((model p).Env Γ) := sorry

/-- `𝔹⊥` has three elements. -/
theorem card_ty_bool : Nat.card ((model p).ty Ty.bool) = 3 := sorry

/-- For a PCF_bool type `σ`, `⟦σ -> τ⟧` is the poset of all monotone maps
`⟦σ⟧ → ⟦τ⟧` (the notes' "truth tables"). -/
noncomputable def arrowOrderIsoOfIsBool {σ : Ty} (hσ : Ty.IsBool σ) (τ : Ty) :
    (model p).ty (σ ⤳ τ) ≃o ((model p).ty σ →o (model p).ty τ) :=
  haveI := finite_ty_of_isBool (p := p) hσ
  continuousHomOrderIso

/-! ### The fragment -/

/-- The operators of PCF_bool: `true`, `false`, `if` at PCF_bool types, and the
primitive divergent term `Ω_bool`. -/
inductive BoolOp : List Ty → Ty → Type
  /-- `true`. -/
  | tt : BoolOp [] .bool
  /-- `false`. -/
  | ff : BoolOp [] .bool
  /-- `if b then t else t'` at a PCF_bool type. -/
  | ite (τ : Ty) (hτ : Ty.IsBool τ) : BoolOp [.bool, τ, τ] τ
  /-- The primitive divergent term `Ω_bool`. -/
  | omega : BoolOp [] .bool

/-- The signature of the PCF_bool operators, over all base types of PCF. Its
terms may bind variables of types mentioning `nat`; the notes' fragment proper
is `pcfBoolSig`. -/
abbrev boolSig : Signature := ⟨Base, BoolOp⟩

/-- Terms over the PCF_bool operators. They may bind variables of any PCF type,
so they form a superset of the notes' PCF_bool terms (`PCFBoolTerm`). -/
abbrev BoolTerm (Γ : Ctx Base) (τ : Ty) : Type := TypedLambda.Term boolSig Γ τ

namespace BoolTerm

variable {Γ : Ctx Base} {τ : Ty}

/-- `true`. -/
abbrev tt : BoolTerm Γ .bool := .op .tt .nil

/-- `false`. -/
abbrev ff : BoolTerm Γ .bool := .op .ff .nil

/-- `if b then t else t'`, at a PCF_bool type. -/
abbrev ite (hτ : Ty.IsBool τ) (b : BoolTerm Γ .bool) (t t' : BoolTerm Γ τ) : BoolTerm Γ τ :=
  .op (.ite τ hτ) (b ::ₜ t ::ₜ t' ::ₜ .nil)

/-- `Ω_bool`. -/
abbrev omega : BoolTerm Γ .bool := .op .omega .nil

end BoolTerm

/-- Each PCF_bool operator as a PCF term over its argument variables; `Ω_bool`
is `fix(fun x:bool. x)`. -/
def BoolOp.toPCF : {as : List Ty} → {τ : Ty} → BoolOp as τ → Term false as τ
  | _, _, .tt => Term.tt
  | _, _, .ff => Term.ff
  | _, _, .ite _ _ => Term.ite (.var .zero) (.var (.succ .zero)) (.var (.succ (.succ .zero)))
  | _, _, .omega => Term.omega .bool

/-- The inclusion of PCF_bool into PCF, a derived-operator translation. -/
abbrev BoolTerm.toPCF {Γ : Ctx Base} {τ : Ty} (t : BoolTerm Γ τ) : Term false Γ τ :=
  TypedLambda.Term.translate BoolOp.toPCF t

namespace BoolTerm

variable {Γ : Ctx Base} {τ : Ty}

/-- `true` is included as PCF's `true`. -/
@[simp] theorem toPCF_tt : toPCF (tt : BoolTerm Γ .bool) = Term.tt := sorry

/-- `false` is included as PCF's `false`. -/
@[simp] theorem toPCF_ff : toPCF (ff : BoolTerm Γ .bool) = Term.ff := sorry

@[simp] theorem toPCF_ite (hτ : Ty.IsBool τ) (b : BoolTerm Γ .bool) (t t' : BoolTerm Γ τ) :
    toPCF (ite hτ b t t') = Term.ite (toPCF b) (toPCF t) (toPCF t') := sorry

end BoolTerm

/-- The denotation of a PCF_bool operator: that of its translation into PCF. -/
noncomputable def BoolOp.denote {as : List Ty} {τ : Ty} (o : BoolOp as τ) :
    TyEnv baseDom as →𝒄 Ty.denote baseDom τ :=
  (model false).denote o.toPCF

/-- The domain model of PCF_bool, the restriction of that of PCF. -/
@[reducible] noncomputable def boolModel : Model boolSig := ⟨baseDom, BoolOp.denote⟩

section Denotation

variable {Γ : Ctx Base} {τ : Ty}

variable (ρ : boolModel.Env Γ)

@[simp] theorem boolModel_denote_tt :
    boolModel.denote (BoolTerm.tt : BoolTerm Γ .bool) ρ = (Part.some true : Part Bool) := sorry

@[simp] theorem boolModel_denote_ff :
    boolModel.denote (BoolTerm.ff : BoolTerm Γ .bool) ρ = (Part.some false : Part Bool) := sorry

/-- `⟦Ω_bool⟧ = ⊥`. -/
@[simp] theorem boolModel_denote_omega :
    boolModel.denote (BoolTerm.omega : BoolTerm Γ .bool) ρ = (⊥ : Part Bool) := sorry

/-- `⟦if b then t else t'⟧ = if(⟦b⟧, ⟦t⟧, ⟦t'⟧)`. -/
theorem boolModel_denote_ite (hτ : Ty.IsBool τ) (b : BoolTerm Γ .bool) (t t' : BoolTerm Γ τ) :
    boolModel.denote (BoolTerm.ite hτ b t t') ρ =
      condFun (boolModel.denote b ρ : Part Bool) (boolModel.denote t ρ)
        (boolModel.denote t' ρ) := sorry

end Denotation

/-! ### Contextual equivalence over the PCF_bool operators -/

/-- The observation of `boolSig`: programs of type `bool`, evaluated through
their inclusion into PCF. -/
def boolObs : Observation boolSig :=
  ⟨fun γ => γ = Ty.bool, fun t v => BoolTerm.toPCF t ⇓ BoolTerm.toPCF v⟩

variable {Γ : Ctx Base} {τ : Ty}

/-- Theorem 24 for `boolSig`: terms over the PCF_bool operators with equal
denotations are contextually equivalent for the contexts of `boolSig`. -/
theorem boolCtxEquiv_of_denote_eq {t u : BoolTerm Γ τ}
    (h : boolModel.denote t = boolModel.denote u) : boolObs.CtxEquiv t u := sorry

/-- There are at most as many `boolSig` terms `Γ ⊢ t : τ` up to contextual
equivalence as elements of the finite domain `⟦Γ⟧ →𝒄 ⟦τ⟧`, for a PCF_bool type
`τ` and context `Γ` of PCF_bool types. -/
theorem card_boolCtxQuot_le (hΓ : ∀ a ∈ Γ, Ty.IsBool a) (hτ : Ty.IsBool τ) :
    Nat.card (boolObs.CtxQuot Γ τ) ≤
      Nat.card ((model false).Env Γ →𝒄 (model false).ty τ) := sorry

/-- There are finitely many `boolSig` terms `Γ ⊢ t : τ` up to contextual
equivalence, for every PCF_bool type `τ` and context `Γ` of PCF_bool types. -/
theorem finite_boolCtxQuot (hΓ : ∀ a ∈ Γ, Ty.IsBool a) (hτ : Ty.IsBool τ) :
    Finite (boolObs.CtxQuot Γ τ) := sorry

/-! ### The notes' fragment -/

/-- The types of PCF_bool proper: simple types over the single base type
`bool`, here `Unit`. -/
abbrev BoolTy : Type := TypedLambda.Ty Unit

/-- A PCF_bool type as a PCF type. -/
def BoolTy.toTy : BoolTy → Ty
  | .base _ => Ty.bool
  | .arrow σ τ => BoolTy.toTy σ ⤳ BoolTy.toTy τ

/-- Every `BoolTy` is a PCF_bool type. -/
theorem BoolTy.isBool_toTy : ∀ τ : BoolTy, Ty.IsBool τ.toTy := sorry

/-- `BoolTy.toTy` is injective. -/
theorem BoolTy.toTy_injective : Function.Injective BoolTy.toTy := sorry

/-- The PCF_bool types are exactly the images of `BoolTy.toTy`. -/
theorem Ty.isBool_iff {τ : Ty} : Ty.IsBool τ ↔ ∃ σ : BoolTy, σ.toTy = τ := sorry

/-- The operators of PCF_bool proper: `true`, `false`, `if` at every PCF_bool
type, and `Ω_bool`. -/
inductive PCFBoolOp : List BoolTy → BoolTy → Type
  /-- `true`. -/
  | tt : PCFBoolOp [] (.base ())
  /-- `false`. -/
  | ff : PCFBoolOp [] (.base ())
  /-- `if b then t else t'`. -/
  | ite (τ : BoolTy) : PCFBoolOp [.base (), τ, τ] τ
  /-- The primitive divergent term `Ω_bool`. -/
  | omega : PCFBoolOp [] (.base ())

/-- The signature of PCF_bool proper, with the single base type `bool`. -/
abbrev pcfBoolSig : Signature := ⟨Unit, PCFBoolOp⟩

/-- The terms of PCF_bool, as in the notes: every variable, subterm and context
has a PCF_bool type. -/
abbrev PCFBoolTerm (Γ : Ctx Unit) (τ : BoolTy) : Type := TypedLambda.Term pcfBoolSig Γ τ

/-- `Ω_bool` in PCF_bool. -/
abbrev PCFBoolTerm.omega {Γ : Ctx Unit} : PCFBoolTerm Γ (.base ()) := .op .omega Tuple.nil

/-- The inclusion of PCF_bool into `boolSig`, translating the types along
`BoolTy.toTy`. -/
def PCFBoolTerm.embed : {Γ : Ctx Unit} → {τ : BoolTy} → PCFBoolTerm Γ τ →
    BoolTerm (Γ.map BoolTy.toTy) τ.toTy
  | _, _, .var x => .var (x.map BoolTy.toTy)
  | _, _, .lam t => .lam (embed t)
  | _, _, .app t u => .app (embed t) (embed u)
  | _, _, .op .tt _ => BoolTerm.tt
  | _, _, .op .ff _ => BoolTerm.ff
  | _, _, .op .omega _ => BoolTerm.omega
  | _, _, .op (.ite τ) args =>
      BoolTerm.ite τ.isBool_toTy (embed (args _ .zero)) (embed (args _ (.succ .zero)))
        (embed (args _ (.succ (.succ .zero))))

/-- The inclusion of PCF_bool into PCF. -/
abbrev PCFBoolTerm.toPCF {Γ : Ctx Unit} {τ : BoolTy} (t : PCFBoolTerm Γ τ) :
    Term false (Γ.map BoolTy.toTy) τ.toTy :=
  BoolTerm.toPCF (PCFBoolTerm.embed t)

/-- The denotations of the PCF_bool operators, on `𝔹⊥` and the function domains
built from it. -/
noncomputable def PCFBoolOp.denote {as : List BoolTy} {τ : BoolTy} :
    PCFBoolOp as τ → (TyEnv (fun _ => baseDom .bool) as →𝒄
      TypedLambda.Ty.denote (fun _ => baseDom .bool) τ)
  | .tt => ContinuousHom.const (Part.some true : Part Bool)
  | .ff => ContinuousHom.const (Part.some false : Part Bool)
  | .omega => ContinuousHom.const (⊥ : Part Bool)
  | .ite _ => cond.comp (pairHom (projHom _ .zero)
      (pairHom (projHom _ (.succ .zero)) (projHom _ (.succ (.succ .zero)))))

/-- The domain model of PCF_bool: `bool` denotes `𝔹⊥`. -/
@[reducible] noncomputable def pcfBoolModel : Model pcfBoolSig :=
  ⟨fun _ => baseDom .bool, PCFBoolOp.denote⟩

/-- Every domain of the model of PCF_bool is finite. -/
instance pcfBool_finite_ty (τ : BoolTy) : Finite (pcfBoolModel.ty τ) :=
  pcfBoolModel.finite_ty (P := fun _ => True) (fun _ _ => Flat.finite (X := Bool))
    (TypedLambda.Ty.allBase_of_forall (fun _ => trivial) τ)

/-- The possible denotations `⟦Γ⟧ →𝒄 ⟦τ⟧` of PCF_bool terms form a finite
set. -/
instance pcfBool_finite_denoteHom (Γ : Ctx Unit) (τ : BoolTy) :
    Finite (pcfBoolModel.Env Γ →𝒄 pcfBoolModel.ty τ) :=
  pcfBoolModel.finite_denoteHom (P := fun _ => True) (fun _ _ => Flat.finite (X := Bool))
    (fun a _ => TypedLambda.Ty.allBase_of_forall (fun _ => trivial) a)
    (TypedLambda.Ty.allBase_of_forall (fun _ => trivial) τ)

namespace PCFBool

/-- The logical relation between the models of PCF_bool and of `boolSig`, along
`BoolTy.toTy`: equality at `bool`, and related arguments to related results at
function types. -/
def Corr : (τ : BoolTy) → pcfBoolModel.ty τ → boolModel.ty τ.toTy → Prop
  | .base _, d, d' => (d : Part Bool) = d'
  | .arrow σ τ, f, f' => ∀ d d', Corr σ d d' →
      Corr τ (Model.app f d) (Model.app (M := boolModel) (σ := BoolTy.toTy σ) f' d')

/-- `⊥` is related to `⊥` at every type. -/
theorem corr_bot : (τ : BoolTy) → Corr τ ⊥ ⊥ := sorry

/-- The conditional preserves the relation. -/
theorem corr_condFun {τ : BoolTy} {c : Part Bool} {x y : pcfBoolModel.ty τ}
    {x' y' : boolModel.ty τ.toTy} (hx : Corr τ x x') (hy : Corr τ y y') :
    Corr τ (condFun c x y) (condFun c x' y') := sorry

/-- Related environments: the value of each variable is related to the value of
its image under the inclusion. -/
def EnvCorr {Γ : Ctx Unit} (ρ : pcfBoolModel.Env Γ)
    (ρ' : boolModel.Env (Γ.map BoolTy.toTy)) : Prop :=
  ∀ a (x : Var Γ a), Corr a (ρ a x) (ρ' _ (x.map BoolTy.toTy))

end PCFBool

/-- The observation of PCF_bool: programs of type `bool`, evaluated through
their inclusion into PCF. -/
def pcfBoolObs : Observation pcfBoolSig :=
  ⟨fun γ => γ = .base (), fun t v => boolObs.Eval (PCFBoolTerm.embed t) (PCFBoolTerm.embed v)⟩

/-- `Ω_bool` diverges in PCF_bool. -/
theorem pcfBoolObs_omega_diverges (v : PCFBoolTerm [] (.base ())) :
    ¬ pcfBoolObs.Eval PCFBoolTerm.omega v := sorry

section PCFBoolCtx

variable {Γ : Ctx Unit} {τ : BoolTy}

/-- Theorem 24 for PCF_bool: PCF_bool terms with equal denotations are
contextually equivalent for the contexts of PCF_bool. -/
theorem pcfBool_ctxEquiv_of_denote_eq {t u : PCFBoolTerm Γ τ}
    (h : pcfBoolModel.denote t = pcfBoolModel.denote u) : pcfBoolObs.CtxEquiv t u := sorry

/-- There are at most as many PCF_bool terms `Γ ⊢ t : τ` up to contextual
equivalence as elements of the finite domain `⟦Γ⟧ →𝒄 ⟦τ⟧`. -/
theorem pcfBool_card_ctxQuot_le :
    Nat.card (pcfBoolObs.CtxQuot Γ τ) ≤
      Nat.card (pcfBoolModel.Env Γ →𝒄 pcfBoolModel.ty τ) := sorry

end PCFBoolCtx

end Silmaril.Calculi.PCF
