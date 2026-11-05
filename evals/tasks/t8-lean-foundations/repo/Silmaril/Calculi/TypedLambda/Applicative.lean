module

public import Silmaril.Calculi.TypedLambda.Context
public import Mathlib.Order.FixedPoints
public import Cslib.Foundations.Semantics.LTS.Bisimulation

/-! # Applicative similarity and bisimilarity

Applicative bisimulation (Abramsky; §8.2 of the notes, p. 69) compares closed
terms by what can be observed of them by applying them to closed arguments
until a ground type is reached. It is defined here for an arbitrary
`Observation` over a signature.

A type-indexed relation on closed terms (`ClosedRel`) is an *applicative
simulation* if related terms of a base type have included results, and
related functions applied to the same closed argument are related. Applicative
similarity `O.AppSim` is the greatest applicative simulation: the greatest
fixed point (Mathlib's `OrderHom.gfp`) of the monotone operator
`ClosedRel.applicative`, which relates terms of base type by a given base
relation and functions when all their applications are related. Applicative
bisimilarity `O.AppBisim` is the greatest fixed point for the base relation
"same results". Both base relations (`Observation.ResultLe`,
`Observation.ResultEquiv`) only constrain base types that the observation
observes (`O.Ground`): at an unobserved base type they hold vacuously, since
nothing can be observed there. For PCF every base type is observed, so this
guard is invisible and the definition is the notes' one.

Since the operator only looks at the relation at strictly smaller types, the
greatest fixed point is its unique fixed point and can be computed by
recursion on types (`ClosedRel.gfp_applicative`, `ClosedRel.lift`): at base
types it is the base relation, and at `σ ⤳ τ` it relates `t` and `t'` iff
`t u` and `t' u` are related for every closed `u`. Bisimilarity is similarity
in both directions (`Observation.appBisim_iff_appSim`).

The contextual preorder of closed terms is an applicative simulation, so it is
included in applicative similarity (`Observation.CtxLe.appSim`). When every
base type is observed, the converse holds exactly when the contextual preorder
of closed terms is extensional: determined by results at base types and by
applications at function types (`Observation.appSim_iff_ctxLe` and, for the
other direction, `Observation.extensional_of_appSim_iff_ctxLe`). It is for PCF
and PCF+`por` (Proposition 39; `PCF.appSim_iff_ctxLe`). All of this concerns
closed terms only; applicative similarity is not extended to open terms here.

Applicative similarity is also CSLib's similarity of a labelled transition
system (`Observation.appLTS`): the states are the closed terms of every type
and a final state; a function has a transition labelled by each closed
argument to its application, and a term of observed base type has a
transition labelled by each of its results to the final state
(`Observation.appSim_iff_similarity`, `Observation.appBisim_iff_bisimilarity`).
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {S : Signature}

/-- Type-indexed relations between closed terms of the same type. They form a
complete lattice. -/
abbrev ClosedRel (S : Signature) : Type :=
  (τ : Ty S.Base) → Term S [] τ → Term S [] τ → Prop

/-- Relations between closed terms of the same base type. -/
abbrev BaseRel (S : Signature) : Type :=
  (b : S.Base) → Term S [] (.base b) → Term S [] (.base b) → Prop

namespace ClosedRel

/-- One step of applicative simulation: terms of base type are related by
`B`, and functions are related by `R` applied to every closed argument. -/
def appStep (B : BaseRel S) (R : ClosedRel S) : ClosedRel S
  | .base b, t, t' => B b t t'
  | .arrow _ τ, t, t' => ∀ u, R τ (.app t u) (.app t' u)

/-- The monotone operator whose post-fixed points are the applicative
simulations for the base relation `B`. -/
def applicative (B : BaseRel S) : ClosedRel S →o ClosedRel S where
  toFun := appStep B
  monotone' _ _ h
    | .base _, _, _, ht => ht
    | .arrow _ τ, _, _, ht => fun u => h τ _ _ (ht u)

/-- The relation defined by recursion on types: `B` at base types, and at
`σ ⤳ τ`, related applications to every closed argument. -/
def lift (B : BaseRel S) : ClosedRel S
  | .base b, t, t' => B b t t'
  | .arrow _ τ, t, t' => ∀ u, lift B τ (.app t u) (.app t' u)

variable {B B' : BaseRel S}

/-- `lift` is monotone in the base relation. -/
theorem lift_mono (h : B ≤ B') : lift B ≤ lift B' := sorry

end ClosedRel

namespace Observation

open ClosedRel

variable (O : Observation S)

/-- At an observed base type, every result of the first term is a result of
the second. -/
def ResultLe : BaseRel S :=
  fun b t t' => O.Ground (.base b) → ∀ v, O.Eval t v → O.Eval t' v

/-- At an observed base type, the two terms have the same results. -/
def ResultEquiv : BaseRel S :=
  fun b t t' => O.Ground (.base b) → ∀ v, O.Eval t v ↔ O.Eval t' v

/-- An applicative simulation: related terms of base type have included
results, and related functions applied to a common closed argument are
related. -/
def IsAppSimulation (R : ClosedRel S) : Prop := R ≤ applicative O.ResultLe R

/-- An applicative bisimulation: related terms of base type have the same
results, and related functions applied to a common closed argument are
related. -/
def IsAppBisimulation (R : ClosedRel S) : Prop := R ≤ applicative O.ResultEquiv R

/-- Applicative similarity `t ≲ t'`: the greatest applicative simulation. -/
def AppSim : ClosedRel S := OrderHom.gfp (applicative O.ResultLe)

/-- Applicative bisimilarity: the greatest applicative bisimulation. -/
def AppBisim : ClosedRel S := OrderHom.gfp (applicative O.ResultEquiv)

variable {O}

/-- Applicative similarity is an applicative simulation. -/
theorem isAppSimulation_appSim : O.IsAppSimulation O.AppSim := sorry

/-- Applicative bisimilarity is an applicative bisimulation. -/
theorem isAppBisimulation_appBisim : O.IsAppBisimulation O.AppBisim := sorry

section Unfold

variable {b : S.Base} {σ τ : Ty S.Base}

end Unfold

variable {τ : Ty S.Base}

/-- Applicative bisimilarity is an equivalence relation at each type. -/
theorem appBisim_equivalence (τ : Ty S.Base) : Equivalence (O.AppBisim τ) := sorry

/-! ### The contextual preorder -/

/-- Contextually equivalent closed terms are applicatively bisimilar. -/
theorem CtxEquiv.appBisim {t t' : Term S [] τ} (h : O.CtxEquiv t t') : O.AppBisim τ t t' := sorry

/-- The converse of `appSim_iff_ctxLe`: if applicative similarity is the
contextual preorder on closed terms, then the contextual preorder is
extensional, i.e. the hypotheses `hbase` and `harrow` of `appSim_iff_ctxLe`
hold. -/
theorem extensional_of_appSim_iff_ctxLe
    (h : ∀ {τ} {t t' : Term S [] τ}, O.AppSim τ t t' ↔ O.CtxLe t t') :
    (∀ {b} {t t' : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval t' v) →
      O.CtxLe t t') ∧
    (∀ {σ τ} {t t' : Term S [] (σ ⤳ τ)},
      (∀ u, O.CtxLe (.app t u) (.app t' u)) → O.CtxLe t t') := sorry

end Observation

/-! ### Applicative similarity as similarity of a labelled transition system -/

/-- The states of the applicative transition system: closed terms of every
type, and a final state reached after a result is observed. -/
inductive AppState (S : Signature) : Type
  /-- A closed term of type `τ`. -/
  | term (τ : Ty S.Base) (t : Term S [] τ)
  /-- The final state. -/
  | done

/-- The labels of the applicative transition system. -/
inductive AppLabel (S : Signature) : Type
  /-- Application to the closed argument `u : σ`. -/
  | app (σ : Ty S.Base) (u : Term S [] σ)
  /-- Observation of the result `v` at the base type `b`. -/
  | result (b : S.Base) (v : Term S [] (.base b))

/-- The relation on states induced by a type-indexed relation on closed terms,
together with the pair of final states. -/
inductive AppState.Rel (R : ClosedRel S) : AppState S → AppState S → Prop
  /-- Terms of the same type related by `R`. -/
  | term {τ : Ty S.Base} {t t' : Term S [] τ} : R τ t t' → Rel R (.term τ t) (.term τ t')
  /-- The two final states. -/
  | done : Rel R .done .done

namespace Observation

variable (O : Observation S)

/-- The transitions of the applicative transition system. -/
inductive AppTr : AppState S → AppLabel S → AppState S → Prop
  /-- `t --u--> t u` for a closed function `t` and closed argument `u`. -/
  | app {σ τ : Ty S.Base} (t : Term S [] (σ ⤳ τ)) (u : Term S [] σ) :
      AppTr (.term _ t) (.app σ u) (.term τ (.app t u))
  /-- `t --v--> done` for a result `v` of `t` at an observed base type. -/
  | result {b : S.Base} {t v : Term S [] (.base b)} :
      O.Ground (.base b) → O.Eval t v → AppTr (.term _ t) (.result b v) .done

/-- The applicative transition system as a CSLib `LTS`. -/
def appLTS : Cslib.LTS (AppState S) (AppLabel S) := ⟨O.AppTr⟩

variable {O}

end Observation

end Silmaril.Calculi.TypedLambda
