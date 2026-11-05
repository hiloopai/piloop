module

public import Silmaril.Calculi.PCF.FullAbstraction
public import Silmaril.Calculi.PCF.Stability
public import Silmaril.Calculi.PCF.BoolFragment
public import Silmaril.Calculi.PCF.Applicative


/-! # §8 Full abstraction (pp. 64–69)

Index of §8.1 and §8.2 of the notes, and of Exercise 20 (p. 69), against
`Silmaril.Calculi.PCF.Por`, `Silmaril.Calculi.PCF.Undefinability` and
`Silmaril.Calculi.PCF.FullAbstraction`, which rest on the generic logical relations
of `Silmaril.Calculi.TypedLambda.LogicalRelation` and Sieber's relation
`Silmaril.Semantics.Domain.SieberRel`. Each wrapper restates the notes' item and is
proved in one step by that development.

The notes omit the proof of Theorem 40 and cite Plotkin's activity lemma,
stable functions, and logical relations. The general development follows the
logical-relation proof with Sieber's ternary relation; see
`Silmaril.Calculi.PCF.Undefinability`.

Theorem 41 uses the notes' ingredients (extensionality, soundness,
Exercise 20 and Theorem 40) in a different order. The notes argue
operationally: `T_b t ⇓ v` forces the three evaluations of `t` tested by
`T_b`, soundness turns them into the equations of Exercise 20 for `⟦t⟧`, so
`⟦t⟧` would be `por`, contradicting Theorem 40. The formalization argues
denotationally first: by Exercise 20 and Theorem 40, `⟦T_b⟧` sends the
denotation of every closed PCF term to `⊥` (`theorem41_denoteClosed`), and
only then soundness, applied to `T_b t` itself, shows that `T_b t` has no
result; extensionality (Proposition 39, indexed in `Adequacy`) concludes
`T_true ≅ctx T_false`.

Beyond Theorem 41, the remark of §8.2 (p. 66) that a context separating
`T_true` and `T_false` needs parallel or is made precise: in PCF+`por` the two
terms are not contextually equivalent (`por_separates_T`).

§8.2 (Theorem 42, full abstraction for PCF+`por`) is proved in
`Silmaril.Calculi.PCF.FullAbstraction`, although the notes leave its proof out of scope.
Every element of `⟦τ⟧` is the supremum of an ω-chain of PCF+`por`-definable
finite elements (`Silmaril.Calculi.PCF.Density`, via the finite projections of
`Silmaril.Calculi.PCF.Projection` and the parallel tests of `Silmaril.Calculi.PCF.Definability`),
and the generic argument of `Silmaril.Calculi.TypedLambda.FullAbstraction` turns this density,
with adequacy, into full abstraction.

The closing remarks of §8.2 (pp. 68–69) are indexed as follows.

* Berry's stable functions (p. 68): `por` is not stable (`por_not_stable`), while the
  denotation of every closed PCF term `t : bool -> bool -> bool` is
  (`pcf_bool₂_stable`); see `Silmaril.Semantics.Domain.Stable` and `Silmaril.Calculi.PCF.Stability`.
  Both are stated for the uncurried maps `𝔹⊥ × 𝔹⊥ → 𝔹⊥`, while the notes speak of
  the curried type `𝔹⊥ → 𝔹⊥ → 𝔹⊥`; the two readings agree because the stable model
  is cartesian closed, which is not formalized. This gives a second proof of
  Theorem 40 (`theorem40_of_stable`). The notes' dI-domains are not formalized:
  stability is stated for maps between partial orders, with Berry's stable order on
  stable maps, but no category of dI-domains is built.
* PCF_bool (pp. 68–69): the fragment is `Silmaril.Calculi.PCF.pcfBoolSig`, whose types
  `BoolTy` are exactly the PCF_bool types (`Ty.isBool_iff`) and whose terms and
  contexts only mention them. Its domains are finite (`pcfBool_finite`), monotone
  maps between them are continuous (`pcfBool_monotone_continuous`), and there are
  finitely many contextual-equivalence classes at each type
  (`pcfBool_finitely_many_classes`); see `Silmaril.Calculi.PCF.BoolFragment` and
  `Silmaril.Calculi.TypedLambda.Observation.SoundAdequate.finite_ctxQuot`.
* Applicative bisimilarity (p. 69) captures contextual equivalence
  (`applicative_bisimilarity_eq_ctxEquiv`); see `Silmaril.Calculi.TypedLambda.Applicative` and
  `Silmaril.Calculi.PCF.Applicative`.

Loader's undecidability theorem and the game-semantics models are cited background and
are not formalized here (see the comments at the end of this module).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.FullAbstraction

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain Silmaril.Calculi.PCF
open Silmaril.Calculi.TypedLambda (Ctx Model)
open scoped Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- Definition 31 (Full abstraction, p. 64): the model is fully abstract if
`t₁ ≅ctx t₂ : τ ⇒ ⟦t₁⟧ = ⟦t₂⟧ ∈ ⟦τ⟧`. -/
abbrev definition31 : Prop := FullyAbstract false

/-- Definition 32 (Parallel or, p. 64): `por : 𝔹⊥ × 𝔹⊥ → 𝔹⊥`. -/
noncomputable abbrev definition32 (x y : Part Bool) : Part Bool := porFun x y

/-- Definition 33 (Left sequential or, p. 64): `or : 𝔹⊥ × 𝔹⊥ → 𝔹⊥`. -/
noncomputable abbrev definition33 (x y : Part Bool) : Part Bool := orFun x y

/-- Definitions 32 and 33 (p. 65): both are boolean `or` on `𝔹`, but
`or(⊥, true) = ⊥` whereas `por(⊥, true) = true`. -/
theorem definition33_ne_definition32 :
    (∀ a b : Bool, definition33 (.some a) (.some b) = definition32 (.some a) (.some b)) ∧
      definition33 .none (.some true) ≠ definition32 .none (.some true) := sorry

/-- Theorem 40 (Undefinability of parallel or, p. 65): there is no closed PCF
term `t : bool -> bool -> bool` with `⟦t⟧ = por : 𝔹⊥ → 𝔹⊥ → 𝔹⊥`. -/
theorem theorem40 : ¬ ∃ t : Closed false Ty.bool₂, denoteClosed t = porElem := sorry

/-- The terms `T_b` (p. 65):
```
T_b = fun f : bool -> (bool -> bool).
        if (f true Ω_bool) then
          if (f Ω_bool true) then
            if (f false false) then Ω_bool else b
          else Ω_bool
        else Ω_bool
```
-/
abbrev T (b : Bool) : Closed false (Ty.bool₂ ⤳ Ty.bool) := Term.tcase b

/-- Theorem 41 (Failure of full abstraction, pp. 65–66): the two terms
`T_true` and `T_false` are contextually equivalent but have different
denotations: `T_true ≅ctx T_false : (bool -> bool -> bool) -> bool` and
`⟦T_true⟧ ≠ ⟦T_false⟧ ∈ (𝔹⊥ → 𝔹⊥ → 𝔹⊥) → 𝔹⊥`. -/
theorem theorem41 : T true ≅ctx T false ∧ denoteClosed (T true) ≠ denoteClosed (T false) := sorry

/-- Theorem 41 (p. 65): the denotational model of PCF, using domains and
continuous functions, is not fully abstract. -/
theorem theorem41_not_fullyAbstract : ¬ definition31 := sorry

/-- Theorem 41, proof (p. 66): `⟦T_b⟧(por) = ⟦b⟧`. -/
theorem theorem41_por (b : Bool) :
    Model.app (denoteClosed (T b)) porElem = denoteClosed (Term.bool b : Closed false _) := sorry

/-- Theorem 41, proof (p. 66): if `⟦t⟧(true)(⊥) = true`, `⟦t⟧(⊥)(true) = true`
and `⟦t⟧(false)(false) = false`, then `⟦t⟧ = por` (Exercise 20), which is
impossible by Theorem 40. So `⟦T_b⟧(⟦t⟧) = ⊥` for every closed PCF `t`. -/
theorem theorem41_denoteClosed (b : Bool) (t : Closed false Ty.bool₂) :
    Model.app (denoteClosed (T b)) (denoteClosed t) = ⊥ := sorry

/-- Theorem 41, proof (p. 66): for every closed `t : bool -> bool -> bool`,
`T_b t` has no result, so equation (18) holds trivially. -/
theorem theorem41_diverges (b : Bool) (t : Closed false Ty.bool₂) : Diverges (.app (T b) t) := sorry

/-- §8.2 (p. 66), a remark going beyond the statement of Theorem 41: contexts
of PCF cannot separate `T_true` and `T_false`, but a context of PCF+`por` can:
`T_b (fun x. fun y. por(x, y)) ⇓ b`. So the inclusion of PCF into PCF+`por`
does not preserve contextual equivalence. -/
theorem por_separates_T : ¬ Term.embed (T true) ≅ctx Term.embed (T false) := sorry

/-- Exercise 20 (p. 69): a monotonic `p : 𝔹⊥ → 𝔹⊥ → 𝔹⊥` with
`p(true)(⊥) = true`, `p(⊥)(true) = true` and `p(false)(false) = false` is
`por`: `p(d₁)(d₂) = por(d₁, d₂)` for all `d₁, d₂ ∈ 𝔹⊥`. -/
theorem exercise20 {p : Part Bool → Part Bool → Part Bool} (hp : Monotone p)
    (hp' : ∀ d, Monotone (p d)) (h₁ : p (.some true) ⊥ = .some true)
    (h₂ : p ⊥ (.some true) = .some true) (h₃ : p (.some false) (.some false) = .some false) :
    ∀ d₁ d₂, p d₁ d₂ = definition32 d₁ d₂ := sorry

/-- Exercise 20, for continuous `p ∈ ⟦bool -> bool -> bool⟧`: then `p = por`. -/
theorem exercise20_continuous {p : (model false).ty Ty.bool₂}
    (h₁ : Model.app₂ p (.some true) ⊥ = .some true)
    (h₂ : Model.app₂ p ⊥ (.some true) = .some true)
    (h₃ : Model.app₂ p (.some false) (.some false) = .some false) : p = porElem := sorry

/-! ## §8.2 Full abstraction for PCF+`por` (p. 67) -/

/-- Theorem 42 (Full abstraction for PCF+`por`, p. 67): "If we extend the semantics of
PCF to PCF+`por` with `⟦por⟧ = por` the resulting denotational semantics is fully
abstract." For closed terms: contextually equivalent closed PCF+`por` terms have equal
denotations, and conversely (by adequacy). -/
theorem theorem42 {τ : Ty} (t₁ t₂ : Closed true τ) :
    t₁ ≅ctx t₂ ↔ denoteClosed t₁ = denoteClosed t₂ := sorry

/-- Theorem 42, for open terms: the model of PCF+`por` is fully abstract in the sense of
Definition 31, `t₁ ≅ctx t₂ : τ ⇒ ⟦t₁⟧ = ⟦t₂⟧ ∈ ⟦Γ⟧ → ⟦τ⟧`. The wrapper `definition31`
is Definition 31 for PCF (`FullyAbstract false`); this is the same definition at the
`por` flag, restated below. -/
theorem theorem42_fullyAbstract : FullyAbstract true := sorry

/-- Theorem 42, preorder form for closed terms: `t₁ ≤ctx t₂ : τ ⇔ ⟦t₁⟧ ⊑ ⟦t₂⟧ ∈ ⟦τ⟧`. -/
theorem theorem42_le_closed {τ : Ty} (t₁ t₂ : Closed true τ) :
    t₁ ≤ctx t₂ ↔ denoteClosed t₁ ≤ denoteClosed t₂ := sorry

/-- Theorem 42, preorder form: `t₁ ≤ctx t₂ : τ ⇔ ⟦t₁⟧ ⊑ ⟦t₂⟧`. -/
theorem theorem42_le {Γ : Ctx Base} {τ : Ty} (t₁ t₂ : Term true Γ τ) :
    t₁ ≤ctx t₂ ↔ denote t₁ ≤ denote t₂ := sorry

/-! ## §8.2 Fully abstract semantics for PCF (p. 68) -/

/-- §8.2 (p. 68): Berry refines the model to stable functions "so that `por` does not
belong to the semantic type `𝔹⊥ → 𝔹⊥ → 𝔹⊥`": parallel or is not stable. Stated for
the uncurried map `𝔹⊥ × 𝔹⊥ → 𝔹⊥`, which corresponds to the curried one in the
cartesian closed stable model. -/
theorem por_not_stable : ¬ Stable fun d : Part Bool × Part Bool => definition32 d.1 d.2 := sorry

/-- §8.2 (p. 68), the other half of Berry's refinement: the denotation of every closed
PCF term `t : bool -> bool -> bool` is a stable function, read uncurried as a map
`𝔹⊥ × 𝔹⊥ → 𝔹⊥` (as in `por_not_stable`). -/
theorem pcf_bool₂_stable (t : Closed false Ty.bool₂) :
    Stable fun d : Part Bool × Part Bool => Model.app₂ (denoteClosed t) d.1 d.2 := sorry

/-! §8.2 (p. 68): the dI-domain refinement "is still not fully abstract". The notes
do not give the counterexample, and it is not formalized. -/

/-! ## §8.2 Undecidability of contextual equivalence (pp. 68–69) -/

/-- §8.2 (p. 68): "the domain associated to each PCF_bool type is finite", for the
PCF_bool types `τ` among the types of PCF. -/
theorem pcfBool_finite {τ : Ty} (hτ : Ty.IsBool τ) : Finite ((model false).ty τ) := sorry

/-- §8.2 (p. 68): "if the domains `D` and `D'` are finite, then all chains are
ultimately constant, and thus all monotone functions from `D` to `D'` are continuous",
for the domains of PCF_bool types. -/
theorem pcfBool_monotone_continuous {σ τ : Ty} (hσ : Ty.IsBool σ)
    {f : (model false).ty σ → (model false).ty τ} (hf : Monotone f) : ωScottContinuous f := sorry

/-- §8.2 (p. 69): "Since there are finitely many semantic terms at each type, there are
also finitely many different equivalence classes up to contextual equivalence": PCF_bool
terms `Γ ⊢ t : τ`, up to contextual equivalence for the contexts of PCF_bool, form a
finite set. -/
theorem pcfBool_finitely_many_classes {Γ : Ctx Unit} {τ : BoolTy} :
    Finite (pcfBoolObs.CtxQuot Γ τ) := sorry

/-! Loader's theorem (p. 69): definability of elements of `⟦τ⟧` by PCF_bool terms, and
contextual equivalence of PCF_bool terms, are undecidable uniformly in the PCF_bool type
`τ`. Not formalized. -/

/-- §8.2 (p. 69): "applicative bisimilarity, a relation which captures contextual
equivalence": on closed PCF terms, applicative bisimilarity is `≅ctx`. The general
result also covers PCF+`por`; open terms are not covered. -/
theorem applicative_bisimilarity_eq_ctxEquiv {τ : Ty} (t₁ t₂ : Closed false τ) :
    AppBisim t₁ t₂ ↔ t₁ ≅ctx t₂ := sorry

/-- §8.2 (p. 69), for the preorder: applicative similarity is `≤ctx`. -/
theorem applicative_similarity_eq_ctxLe {τ : Ty} (t₁ t₂ : Closed false τ) :
    AppSim t₁ t₂ ↔ t₁ ≤ctx t₂ := sorry

/-! Game semantics (p. 68): the fully abstract models of Abramsky, Jagadeesan and
Malacaria and of Hyland and Ong are not yet formalized. O'Hearn and Riecke's
Kripke-logical-relation model is not formalized. -/

end Silmaril.Courses.Cambridge.DenotationalSemantics.FullAbstraction
