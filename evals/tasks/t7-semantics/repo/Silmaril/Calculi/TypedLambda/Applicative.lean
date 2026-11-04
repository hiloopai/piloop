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

/-- `lift B` is a fixed point of the applicative operator. -/
theorem applicative_lift : applicative B (lift B) = lift B := by
  funext τ
  cases τ <;> rfl

/-- Every applicative simulation (post-fixed point) is included in `lift B`. -/
theorem le_lift {R : ClosedRel S} (h : R ≤ applicative B R) : R ≤ lift B := by
  intro τ
  induction τ with
  | base b => exact fun t t' hr => h _ t t' hr
  | arrow σ τ _ ih => exact fun t t' hr u => ih _ _ (h _ t t' hr u)

/-- The greatest fixed point of the applicative operator is the relation
defined by recursion on types. -/
theorem gfp_applicative : OrderHom.gfp (applicative B) = lift B :=
  le_antisymm (le_lift (OrderHom.map_gfp _).ge) (OrderHom.le_gfp _ applicative_lift.ge)

/-- `lift` is monotone in the base relation. -/
theorem lift_mono (h : B ≤ B') : lift B ≤ lift B' := by
  intro τ
  induction τ with
  | base b => exact h b
  | arrow σ τ _ ih => exact fun t t' ht u => ih _ _ (ht u)

/-- `lift` commutes with meets of base relations. -/
theorem lift_inf_iff {τ : Ty S.Base} {t t' : Term S [] τ} :
    lift (B ⊓ B') τ t t' ↔ lift B τ t t' ∧ lift B' τ t t' := by
  induction τ with
  | base b => exact Iff.rfl
  | arrow σ τ _ ih => exact (forall_congr' fun _ => ih).trans forall_and

/-- `lift` commutes with taking converses. -/
theorem lift_flip {τ : Ty S.Base} {t t' : Term S [] τ} :
    lift (fun b => flip (B b)) τ t t' ↔ lift B τ t' t := by
  induction τ with
  | base b => rfl
  | arrow σ τ _ ih => exact forall_congr' fun _ => ih

/-- `lift` of a reflexive base relation is reflexive. -/
theorem lift_refl (h : ∀ b t, B b t t) {τ : Ty S.Base} (t : Term S [] τ) : lift B τ t t := by
  induction τ with
  | base b => exact h b t
  | arrow σ τ _ ih => exact fun _ => ih _

/-- `lift` of a transitive base relation is transitive. -/
theorem lift_trans (h : ∀ b t₁ t₂ t₃, B b t₁ t₂ → B b t₂ t₃ → B b t₁ t₃) {τ : Ty S.Base}
    {t₁ t₂ t₃ : Term S [] τ} : lift B τ t₁ t₂ → lift B τ t₂ t₃ → lift B τ t₁ t₃ := by
  induction τ with
  | base b => exact h b t₁ t₂ t₃
  | arrow σ τ _ ih => exact fun h₁ h₂ u => ih (h₁ u) (h₂ u)

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

/-- Applicative similarity is the relation defined by recursion on types. -/
theorem appSim_eq_lift : O.AppSim = lift O.ResultLe := gfp_applicative

/-- Applicative bisimilarity is the relation defined by recursion on types. -/
theorem appBisim_eq_lift : O.AppBisim = lift O.ResultEquiv := gfp_applicative

/-- Applicative similarity is an applicative simulation. -/
theorem isAppSimulation_appSim : O.IsAppSimulation O.AppSim := (OrderHom.map_gfp _).ge

/-- Applicative bisimilarity is an applicative bisimulation. -/
theorem isAppBisimulation_appBisim : O.IsAppBisimulation O.AppBisim := (OrderHom.map_gfp _).ge

/-- Coinduction: terms related by an applicative simulation are applicatively
similar. -/
theorem AppSim.of_isAppSimulation {R : ClosedRel S} (hR : O.IsAppSimulation R)
    {τ : Ty S.Base} {t t' : Term S [] τ} (h : R τ t t') : O.AppSim τ t t' :=
  OrderHom.le_gfp _ hR τ t t' h

/-- Coinduction: terms related by an applicative bisimulation are
applicatively bisimilar. -/
theorem AppBisim.of_isAppBisimulation {R : ClosedRel S} (hR : O.IsAppBisimulation R)
    {τ : Ty S.Base} {t t' : Term S [] τ} (h : R τ t t') : O.AppBisim τ t t' :=
  OrderHom.le_gfp _ hR τ t t' h

section Unfold

variable {b : S.Base} {σ τ : Ty S.Base}

/-- At a base type, `t ≲ t'` iff, if the type is observed, every result of `t`
is a result of `t'`. -/
theorem appSim_base_iff {t t' : Term S [] (.base b)} :
    O.AppSim (.base b) t t' ↔ (O.Ground (.base b) → ∀ v, O.Eval t v → O.Eval t' v) := by
  rw [appSim_eq_lift]; rfl

/-- At a function type, `t ≲ t'` iff `t u ≲ t' u` for every closed `u`. -/
theorem appSim_arrow_iff {t t' : Term S [] (σ ⤳ τ)} :
    O.AppSim (σ ⤳ τ) t t' ↔ ∀ u, O.AppSim τ (.app t u) (.app t' u) := by
  rw [appSim_eq_lift]; rfl

/-- At a base type, applicative bisimilarity means, if the type is observed,
having the same results. -/
theorem appBisim_base_iff {t t' : Term S [] (.base b)} :
    O.AppBisim (.base b) t t' ↔ (O.Ground (.base b) → ∀ v, O.Eval t v ↔ O.Eval t' v) := by
  rw [appBisim_eq_lift]; rfl

/-- At a function type, `t` and `t'` are applicatively bisimilar iff `t u` and
`t' u` are, for every closed `u`. -/
theorem appBisim_arrow_iff {t t' : Term S [] (σ ⤳ τ)} :
    O.AppBisim (σ ⤳ τ) t t' ↔ ∀ u, O.AppBisim τ (.app t u) (.app t' u) := by
  rw [appBisim_eq_lift]; rfl

end Unfold

variable {τ : Ty S.Base}

/-- Applicative similarity is reflexive. -/
theorem AppSim.refl (t : Term S [] τ) : O.AppSim τ t t := by
  rw [appSim_eq_lift]
  exact lift_refl (fun _ _ _ _ h => h) t

/-- Applicative similarity is transitive. -/
theorem AppSim.trans {t₁ t₂ t₃ : Term S [] τ} (h₁ : O.AppSim τ t₁ t₂) (h₂ : O.AppSim τ t₂ t₃) :
    O.AppSim τ t₁ t₃ := by
  rw [appSim_eq_lift] at *
  exact lift_trans (fun _ _ _ _ h₁ h₂ hg v hv => h₂ hg v (h₁ hg v hv)) h₁ h₂

/-- Applicative bisimilarity is applicative similarity in both directions. -/
theorem appBisim_iff_appSim {t t' : Term S [] τ} :
    O.AppBisim τ t t' ↔ O.AppSim τ t t' ∧ O.AppSim τ t' t := by
  have hB : O.ResultEquiv = O.ResultLe ⊓ fun b => flip (O.ResultLe b) := by
    funext b t t'
    exact propext ⟨fun h => ⟨fun hg v => (h hg v).1, fun hg v => (h hg v).2⟩,
      fun h hg v => ⟨h.1 hg v, h.2 hg v⟩⟩
  rw [appBisim_eq_lift, appSim_eq_lift, hB, lift_inf_iff]
  exact and_congr Iff.rfl lift_flip

/-- Applicative bisimilarity is reflexive. -/
theorem AppBisim.refl (t : Term S [] τ) : O.AppBisim τ t t :=
  appBisim_iff_appSim.2 ⟨.refl t, .refl t⟩

/-- Applicative bisimilarity is symmetric. -/
theorem AppBisim.symm {t t' : Term S [] τ} (h : O.AppBisim τ t t') : O.AppBisim τ t' t :=
  appBisim_iff_appSim.2 (appBisim_iff_appSim.1 h).symm

/-- Applicative bisimilarity is transitive. -/
theorem AppBisim.trans {t₁ t₂ t₃ : Term S [] τ} (h₁ : O.AppBisim τ t₁ t₂)
    (h₂ : O.AppBisim τ t₂ t₃) : O.AppBisim τ t₁ t₃ :=
  appBisim_iff_appSim.2
    ⟨(appBisim_iff_appSim.1 h₁).1.trans (appBisim_iff_appSim.1 h₂).1,
      (appBisim_iff_appSim.1 h₂).2.trans (appBisim_iff_appSim.1 h₁).2⟩

/-- Applicative bisimilarity is an equivalence relation at each type. -/
theorem appBisim_equivalence (τ : Ty S.Base) : Equivalence (O.AppBisim τ) :=
  ⟨AppBisim.refl, AppBisim.symm, AppBisim.trans⟩

/-! ### The contextual preorder -/

/-- The contextual preorder on closed terms is an applicative simulation. -/
theorem isAppSimulation_ctxLe : O.IsAppSimulation fun _ t t' => O.CtxLe t t'
  | .base _, _, _, h => fun hg _ hv => h.eval hg hv
  | .arrow _ _, _, _, h => fun u => h.app u

/-- Closed terms related by the contextual preorder are applicatively
similar. -/
theorem CtxLe.appSim {t t' : Term S [] τ} (h : O.CtxLe t t') : O.AppSim τ t t' :=
  AppSim.of_isAppSimulation isAppSimulation_ctxLe h

/-- Contextually equivalent closed terms are applicatively bisimilar. -/
theorem CtxEquiv.appBisim {t t' : Term S [] τ} (h : O.CtxEquiv t t') : O.AppBisim τ t t' :=
  appBisim_iff_appSim.2
    ⟨(ctxEquiv_iff_le.1 h).1.appSim, (ctxEquiv_iff_le.1 h).2.appSim⟩

/-- If every base type is observed, closed terms of base type with included
results are contextually ordered, and the contextual preorder of closed
functions is tested on closed arguments, then applicative similarity implies
the contextual preorder on closed terms. -/
theorem ctxLe_of_appSim (hG : ∀ b, O.Ground (.base b))
    (hbase : ∀ {b} {t t' : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval t' v) →
      O.CtxLe t t')
    (harrow : ∀ {σ τ} {t t' : Term S [] (σ ⤳ τ)},
      (∀ u, O.CtxLe (.app t u) (.app t' u)) → O.CtxLe t t')
    {t t' : Term S [] τ} (h : O.AppSim τ t t') : O.CtxLe t t' := by
  induction τ with
  | base b => exact hbase (appSim_base_iff.1 h (hG b))
  | arrow σ τ _ ih => exact harrow fun u => ih (appSim_arrow_iff.1 h u)

/-- Under the hypotheses of `ctxLe_of_appSim` (an extensional contextual
preorder), applicative similarity is the contextual preorder on closed
terms. -/
theorem appSim_iff_ctxLe (hG : ∀ b, O.Ground (.base b))
    (hbase : ∀ {b} {t t' : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval t' v) →
      O.CtxLe t t')
    (harrow : ∀ {σ τ} {t t' : Term S [] (σ ⤳ τ)},
      (∀ u, O.CtxLe (.app t u) (.app t' u)) → O.CtxLe t t')
    {t t' : Term S [] τ} : O.AppSim τ t t' ↔ O.CtxLe t t' :=
  ⟨ctxLe_of_appSim hG hbase harrow, CtxLe.appSim⟩

/-- The converse of `appSim_iff_ctxLe`: if applicative similarity is the
contextual preorder on closed terms, then the contextual preorder is
extensional, i.e. the hypotheses `hbase` and `harrow` of `appSim_iff_ctxLe`
hold. -/
theorem extensional_of_appSim_iff_ctxLe
    (h : ∀ {τ} {t t' : Term S [] τ}, O.AppSim τ t t' ↔ O.CtxLe t t') :
    (∀ {b} {t t' : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval t' v) →
      O.CtxLe t t') ∧
    (∀ {σ τ} {t t' : Term S [] (σ ⤳ τ)},
      (∀ u, O.CtxLe (.app t u) (.app t' u)) → O.CtxLe t t') :=
  ⟨fun hv => h.1 (appSim_base_iff.2 fun _ => hv),
    fun hu => h.1 (appSim_arrow_iff.2 fun u => h.2 (hu u))⟩

/-- Under the hypotheses of `ctxLe_of_appSim`, applicative bisimilarity is
contextual equivalence on closed terms. -/
theorem appBisim_iff_ctxEquiv (hG : ∀ b, O.Ground (.base b))
    (hbase : ∀ {b} {t t' : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval t' v) →
      O.CtxLe t t')
    (harrow : ∀ {σ τ} {t t' : Term S [] (σ ⤳ τ)},
      (∀ u, O.CtxLe (.app t u) (.app t' u)) → O.CtxLe t t')
    {t t' : Term S [] τ} : O.AppBisim τ t t' ↔ O.CtxEquiv t t' := by
  rw [appBisim_iff_appSim, appSim_iff_ctxLe hG hbase harrow,
    appSim_iff_ctxLe hG hbase harrow, ctxEquiv_iff_le]

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

/-- A closed function has one transition per closed argument, to its
application. -/
theorem appTr_app_iff {σ τ : Ty S.Base} {t : Term S [] (σ ⤳ τ)} {u : Term S [] σ}
    {s : AppState S} : O.AppTr (.term _ t) (.app σ u) s ↔ s = .term τ (.app t u) :=
  ⟨fun h => by cases h; rfl, fun h => h ▸ .app t u⟩

/-- A term of base type has a transition labelled `v` exactly when its type is
observed and `v` is a result; it leads to the final state. -/
theorem appTr_result_iff {b : S.Base} {t v : Term S [] (.base b)} {s : AppState S} :
    O.AppTr (.term _ t) (.result b v) s ↔ O.Ground (.base b) ∧ O.Eval t v ∧ s = .done :=
  ⟨fun h => by cases h with | result hg hv => exact ⟨hg, hv, rfl⟩,
    fun ⟨hg, hv, h⟩ => h ▸ .result hg hv⟩

/-- The final state has no transitions. -/
theorem not_appTr_done {μ : AppLabel S} {s : AppState S} : ¬ O.AppTr .done μ s :=
  fun h => nomatch h

open Cslib.LTS in
/-- Applicative similarity is CSLib's similarity for the applicative
transition system. -/
theorem appSim_iff_similarity {τ : Ty S.Base} {t t' : Term S [] τ} :
    O.AppSim τ t t' ↔ Similarity O.appLTS O.appLTS (.term τ t) (.term τ t') := by
  constructor
  · intro h
    refine ⟨AppState.Rel O.AppSim, .term h, ?_⟩
    intro s₁ s₂ hr μ s₁' htr
    cases hr with
    | term hr =>
        cases htr with
        | app t u => exact ⟨_, .app _ u, .term (appSim_arrow_iff.1 hr u)⟩
        | result hg hv => exact ⟨_, .result hg (appSim_base_iff.1 hr hg _ hv), .done⟩
    | done => exact (not_appTr_done htr).elim
  · intro h
    refine AppSim.of_isAppSimulation (R := fun τ t t' =>
      Similarity O.appLTS O.appLTS (.term τ t) (.term τ t')) ?_ h
    rintro (b | ⟨σ, τ⟩) t t' ⟨r, hr, hsim⟩
    · intro hg v hv
      obtain ⟨s, hs, -⟩ := hsim hr _ _ (AppTr.result hg hv)
      exact (appTr_result_iff.1 hs).2.1
    · intro u
      obtain ⟨s, hs, hr'⟩ := hsim hr _ _ (AppTr.app t u)
      rw [appTr_app_iff.1 hs] at hr'
      exact ⟨r, hr', hsim⟩

open Cslib.LTS in
/-- Applicative bisimilarity is CSLib's bisimilarity for the applicative
transition system. -/
theorem appBisim_iff_bisimilarity {τ : Ty S.Base} {t t' : Term S [] τ} :
    O.AppBisim τ t t' ↔ Bisimilarity O.appLTS O.appLTS (.term τ t) (.term τ t') := by
  constructor
  · intro h
    refine ⟨AppState.Rel O.AppBisim, .term h, ?_⟩
    intro s₁ s₂ hr μ
    cases hr with
    | term hr =>
        refine ⟨fun s₁' htr => ?_, fun s₂' htr => ?_⟩
        · cases htr with
          | app t u => exact ⟨_, .app _ u, .term (appBisim_arrow_iff.1 hr u)⟩
          | result hg hv =>
              exact ⟨_, .result hg ((appBisim_base_iff.1 hr hg _).1 hv), .done⟩
        · cases htr with
          | app t' u => exact ⟨_, .app _ u, .term (appBisim_arrow_iff.1 hr u)⟩
          | result hg hv =>
              exact ⟨_, .result hg ((appBisim_base_iff.1 hr hg _).2 hv), .done⟩
    | done =>
        exact ⟨fun _ htr => (not_appTr_done htr).elim, fun _ htr => (not_appTr_done htr).elim⟩
  · intro h
    refine AppBisim.of_isAppBisimulation (R := fun τ t t' =>
      Bisimilarity O.appLTS O.appLTS (.term τ t) (.term τ t')) ?_ h
    rintro (b | ⟨σ, τ⟩) t t' ⟨r, hr, hbis⟩
    · intro hg v
      constructor
      · intro hv
        obtain ⟨s, hs, -⟩ := (hbis hr _).1 _ (AppTr.result hg hv)
        exact (appTr_result_iff.1 hs).2.1
      · intro hv
        obtain ⟨s, hs, -⟩ := (hbis hr _).2 _ (AppTr.result hg hv)
        exact (appTr_result_iff.1 hs).2.1
    · intro u
      obtain ⟨s, hs, hr'⟩ := (hbis hr _).1 _ (AppTr.app t u)
      rw [appTr_app_iff.1 hs] at hr'
      exact ⟨r, hr', hbis⟩

end Observation

end Silmaril.Calculi.TypedLambda
