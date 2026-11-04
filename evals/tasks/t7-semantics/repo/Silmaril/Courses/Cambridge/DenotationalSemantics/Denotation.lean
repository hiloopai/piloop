module

public import Silmaril.Calculi.PCF.Adequacy
public import Silmaril.Courses.Cambridge.DenotationalSemantics.PCF


/-! # §6 Denotational semantics for PCF (pp. 47–55)

Index of §6 of the notes against `Silmaril.Calculi.TypedLambda.Denotation` (the model of
any typed λ-calculus over a signature) and `Silmaril.Calculi.PCF.Denotation` and
`Silmaril.Calculi.PCF.Soundness` (the PCF instance). The wrappers specialize to PCF,
`Term false`; the general theorems also cover PCF+`por`.

A term denotes a bundled continuous map `(model false).Env Γ →𝒄 (model false).ty τ`,
defined by structural recursion, so it is well defined and continuous by
construction (Theorem 25). An element of `⟦σ -> τ⟧` is applied with
`Model.app`. Environments are tuples indexed by the positions of the context
(Definition 23, Remark 9).

Theorem 24 is stated in §6.1 but its proof uses adequacy (Theorem 31), which
is `Silmaril.Calculi.PCF.Adequacy`; see also the §7 index module `Adequacy`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.Denotation

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain Silmaril.Calculi.PCF
open Silmaril.Calculi.TypedLambda (Ctx Var Raw Model Tuple)
open scoped Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {Γ : Ctx Base} {σ τ : Ty}

/-! ## §6.1 Introducing denotational semantics -/

/-- Example 20 (p. 47): `v = fun x:nat. (fun y:nat. y) 𝟶` and `v' = fun x:nat. 𝟶`
have the same denotation, yet `v ⇓ v'` fails, so adequacy cannot hold at
function types. -/
theorem example20 :
    denote (lamBetaZero false) = denote (lamZero false) ∧ ¬ lamBetaZero false ⇓ lamZero false := sorry

/-- Theorem 24 (Semantic equality implies contextual equivalence, p. 48): for
closed `t₁, t₂ ∈ Pcf_τ`, if `⟦t₁⟧ = ⟦t₂⟧ ∈ ⟦τ⟧` then `t₁ ≅ctx t₂ : τ`. -/
theorem theorem24 {t₁ t₂ : Closed false τ}
    (h : (model false).denoteClosed t₁ = (model false).denoteClosed t₂) : t₁ ≅ctx t₂ := sorry

/-- Theorem 24 generalises to open terms (p. 48): if `⟦t₁⟧ = ⟦t₂⟧ : ⟦Γ⟧ → ⟦τ⟧`
then `Γ ⊢ t₁ ≅ctx t₂ : τ`. -/
theorem theorem24_open {t₁ t₂ : Term false Γ τ} (h : denote t₁ = denote t₂) : t₁ ≅ctx t₂ := sorry

/-- Theorem 24, proof (p. 48): the argument uses only soundness,
compositionality (which holds in every model) and adequacy. So in any model
`M` of a typed λ-calculus, for any notion `O` of ground types and evaluation,
if evaluation at ground types is sound and adequate then terms with equal
denotations are contextually equivalent. Adequacy is taken in the form
`⟦v⟧ ⊑ ⟦t⟧ ⇒ t ⇓ v` for results `v`, which the proof for the contextual
preorder needs. -/
theorem theorem24_generic {S : TypedLambda.Signature} {M : Model S} {O : TypedLambda.Observation S}
    {Δ : Ctx S.Base} {α : TypedLambda.Ty S.Base}
    (sound : ∀ {γ}, O.Ground γ → ∀ {t v : TypedLambda.Term S [] γ}, O.Eval t v →
      M.denoteClosed t = M.denoteClosed v)
    (adequate : ∀ {γ}, O.Ground γ → ∀ {t t' v : TypedLambda.Term S [] γ}, O.Eval t' v →
      M.denoteClosed v ≤ M.denoteClosed t → O.Eval t v)
    {t₁ t₂ : TypedLambda.Term S Δ α} (h : M.denote t₁ = M.denote t₂) : O.CtxEquiv t₁ t₂ := sorry

/-! ## §6.2 Definition -/

/-- Definition 22 (Semantics of types, p. 48): `⟦nat⟧ = ℕ⊥`, `⟦bool⟧ = 𝔹⊥` and
`⟦τ -> τ'⟧ = ⟦τ⟧ → ⟦τ'⟧`, the domain of continuous functions. -/
abbrev definition22 (τ : Ty) : Type := (model false).ty τ

/-- Definition 23 (Semantics of context, p. 49): `⟦Γ⟧ = ∏_{x ∈ dom(Γ)} ⟦Γ(x)⟧`,
one value for each variable. -/
abbrev definition23 (Γ : Ctx Base) : Type := (model false).Env Γ

/-- Remark 9 (p. 49): `⟦⋅⟧` has a single element, and `⟦Γ, x:τ⟧ ≅ ⟦Γ⟧ × ⟦τ⟧`
as orders (with the components swapped: an environment for `τ :: Γ` is
`d ::ₜ ρ`). -/
theorem remark9 :
    (∀ ρ : definition23 [], ρ = Tuple.nil) ∧
      Nonempty (definition23 (τ :: Γ) ≃o definition22 τ × definition23 Γ) := sorry

/-- Remark 10 (p. 49): a term has at most one type, so defining the denotation
on terms or on typing derivations is the same (see also
`Pcf.definition17_equiv`). -/
theorem remark10 {t : Raw (sig false)} {τ' : Ty} (h : Raw.HasType Γ t τ)
    (h' : Raw.HasType Γ t τ') : τ = τ' := sorry

/-- Definition 24 (Denotation of operations on `𝔹` and `ℕ`, p. 50), with
`pred : ℕ ⇀ ℕ` undefined at `0` and `f⊥` the flat lifting of Proposition 7. -/
theorem definition24 (ρ : definition23 Γ) (n : Term false Γ .nat) (b : Term false Γ .bool)
    (t t' : Term false Γ τ) :
    denote (Term.zero : Term false Γ .nat) ρ = (Part.some 0 : Part ℕ) ∧
      denote (Term.tt : Term false Γ .bool) ρ = (Part.some true : Part Bool) ∧
      denote (Term.ff : Term false Γ .bool) ρ = (Part.some false : Part Bool) ∧
      denote (Term.succ n) ρ = Flat.map Nat.succ (denote n ρ : Part ℕ) ∧
      denote (Term.pred n) ρ = Flat.lift predPart (denote n ρ : Part ℕ) ∧
      denote (Term.isZero n) ρ = Flat.map (fun k : ℕ => k == 0) (denote n ρ : Part ℕ) ∧
      denote (Term.ite b t t') ρ = condFun (denote b ρ : Part Bool) (denote t ρ) (denote t' ρ) := sorry

/-- Remark 11 (p. 50): the semantic operations of Definition 24 are continuous:
the flat liftings (Proposition 7) and the conditional (Proposition 11). -/
theorem remark11 {D : Type*} [OmegaCompletePartialOrder D] [OrderBot D] :
    ωScottContinuous (Flat.map Nat.succ) ∧ ωScottContinuous (Flat.lift predPart) ∧
      ωScottContinuous (Flat.map fun k : ℕ => k == 0) ∧
      ωScottContinuous fun p : Part Bool × (D × D) => condFun p.1 p.2.1 p.2.2 := sorry

/-- Definition 25 (Denotation of the λ-calculus operations, p. 50):
`⟦x⟧(ρ) = ρ(x)`, `⟦t₁ t₂⟧(ρ) = ⟦t₁⟧(ρ)(⟦t₂⟧(ρ))` and
`⟦fun x:σ. t⟧(ρ) = λd ∈ ⟦σ⟧. ⟦t⟧(ρ[x ↦ d])`. -/
theorem definition25 (ρ : definition23 Γ) (x : Var Γ τ) (t₁ : Term false Γ (σ ⤳ τ))
    (t₂ : Term false Γ σ) (t : Term false (σ :: Γ) τ) (d : definition22 σ) :
    denote (.var x) ρ = ρ τ x ∧
      denote (.app t₁ t₂) ρ = Model.app (denote t₁ ρ) (denote t₂ ρ) ∧
      Model.app (denote (.lam t) ρ) d = denote t (d ::ₜ ρ) := sorry

/-- Definition 26 (Denotation of fixed points, p. 51): `⟦fix f⟧(ρ) = fix(⟦f⟧(ρ))`. -/
theorem definition26 (ρ : definition23 Γ) (f : Term false Γ (τ ⤳ τ)) :
    denote (Term.fix f) ρ = fix (Model.fn (denote f ρ)) := sorry

/-- Theorem 25 (Denotation is well-defined, p. 51): `⟦t⟧ : ⟦Γ⟧ → ⟦τ⟧` is a
continuous function. -/
theorem theorem25 (t : Term false Γ τ) :
    ωScottContinuous (denote t : definition23 Γ → definition22 τ) := sorry

/-- Remark 12 (Denotation of closed terms, p. 51): since `⟦⋅⟧` is a singleton,
a closed term denotes an element `⟦t⟧ ∈ ⟦τ⟧`. -/
theorem remark12 (t : Closed false τ) (ρ : definition23 []) :
    denote t ρ = (model false).denoteClosed t := sorry

/-! ## §6.3 Compositionality -/

/-- Theorem 26 (Compositionality, p. 51): if `⟦t⟧ = ⟦u⟧ : ⟦Δ⟧ → ⟦σ⟧` and
`Γ ⊢_{Δ,σ} C : τ`, then `⟦C[t]⟧ = ⟦C[u]⟧ : ⟦Γ⟧ → ⟦τ⟧`. -/
theorem theorem26 {Δ : Ctx Base} {t u : Term false Δ σ} (h : denote t = denote u)
    (C : Pcf.definition20 Δ σ Γ τ) : denote (C.plug t) = denote (C.plug u) := sorry

/-- Remark 13 (p. 52): a context denotes a map `⟦C⟧` from denotations of the
hole to denotations of the whole, with `⟦C[t]⟧ = ⟦C⟧(⟦t⟧)`. -/
theorem remark13 {Δ : Ctx Base} (C : Pcf.definition20 Δ σ Γ τ)
    (t : Term false Δ σ) : denote (C.plug t) = TypedLambda.Context.denote (model false) C (denote t) := sorry

/-- Proposition 27 (Substitution property of the semantic function, p. 53):
`⟦t[u/x]⟧(ρ) = ⟦t⟧(ρ[x ↦ ⟦u⟧(ρ)])`, and `⟦t[u/x]⟧ = ⟦t⟧(⟦u⟧)` for `Γ = ⋅`. -/
theorem proposition27 (t : Term false (σ :: Γ) τ) (u : Term false Γ σ) (ρ : definition23 Γ)
    (t₀ : Term false [σ] τ) (u₀ : Closed false σ) :
    denote (t.subst1 u) ρ = denote t (denote u ρ ::ₜ ρ) ∧
      (model false).denoteClosed (t₀.subst1 u₀) =
        Model.app (denote (.lam t₀) Tuple.nil) ((model false).denoteClosed u₀) := sorry

/-! ## §6.4 Soundness -/

/-- Theorem 28 (Soundness, p. 53): if `t ⇓τ v` then `⟦t⟧ = ⟦v⟧ ∈ ⟦τ⟧`. -/
theorem theorem28 {t v : Closed false τ} (h : t ⇓ v) :
    (model false).denoteClosed t = (model false).denoteClosed v := sorry

/-- Proposition 29 (Divergence, p. 54): if `t ∈ Pcf_nat` and `⟦t⟧ = ⊥` then
`t ⇑nat`, and similarly for `bool`. -/
theorem proposition29 (t : Closed false .nat) (b : Closed false .bool) :
    ((model false).denoteClosed t = ⊥ → Diverges t) ∧
      ((model false).denoteClosed b = ⊥ → Diverges b) := sorry

/-! ## §6.5 Exercises -/

/-- Exercise 14 (p. 55): the substitution property (Proposition 27). -/
theorem exercise14 (t : Term false (σ :: Γ) τ) (u : Term false Γ σ) (ρ : definition23 Γ) :
    denote (t.subst1 u) ρ = denote t (denote u ρ ::ₜ ρ) := sorry

/-- Exercise 15 (p. 55): `⟦Ωτ⟧` is the least element of `⟦τ⟧`, and
`⟦fun x:τ. Ωτ⟧ = ⟦Ω_{τ -> τ}⟧`. -/
theorem exercise15 (ρ : definition23 Γ) :
    denote (Term.omega τ : Term false Γ τ) ρ = ⊥ ∧
      denote (.lam (Term.omega τ) : Term false Γ (τ ⤳ τ)) =
        denote (Term.omega (τ ⤳ τ) : Term false Γ _) := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.Denotation
