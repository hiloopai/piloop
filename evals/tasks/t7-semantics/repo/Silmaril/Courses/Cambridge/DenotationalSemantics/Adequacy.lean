module

public import Silmaril.Calculi.PCF.Extensionality
public import Silmaril.Courses.Cambridge.DenotationalSemantics.Denotation


/-! # §7 Adequacy (pp. 56–63)

Index of §7 of the notes against `Silmaril.Calculi.PCF.Adequacy` (§7.1–7.2) and
`Silmaril.Calculi.PCF.Extensionality` (§7.3). The wrappers specialize to PCF,
`Term false`; the general theorems also cover PCF+`por`, for which the notes
give no argument.

The fundamental property (Theorem 30) is proved as in the notes, by induction
on terms, with one lemma per operator about closed terms (the notes'
equations (9) and (10)); the case of `fix` is Scott induction. A parallel
closed substitution (Definition 28) is a tuple of closed terms, one per
variable of the context.

In §7.3, the proof of Lemma 37 follows the notes. It needs closed PCF
functions `g : γ -> bool` testing whether their argument evaluates to a given
ground value; the notes appeal to Turing completeness, and the general
development uses a conditional for booleans and a recursion on the numeral
with `zero?` and `pred` for numbers (`Silmaril.Calculi.PCF.Term.testBool`,
`Silmaril.Calculi.PCF.Term.testNum`). The notes' `f = fun x:τ. g (c x)` with
`c = fun x:τ. C[x]` is formalized with `c x` already β-reduced, as the closing
`fun x:τ. g C[x]` of the context `g C` (`Silmaril.Calculi.TypedLambda.Context.close`).

The second example of Remark 14 (a denotational equality that holds exactly
when a PCF function is total) is not formalized.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.Adequacy

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain Silmaril.Calculi.PCF
open Silmaril.Calculi.TypedLambda (Ctx Var Model Tuple Subst)
open Silmaril.Courses.Cambridge.DenotationalSemantics.Denotation (definition22 definition23)
open scoped Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {Γ : Ctx Base} {σ τ : Ty}

/-- Remark 14 (Adequacy at function types, p. 56): adequacy fails at function
types. `⟦fun x:τ. (fun y:τ. y) x⟧ = ⟦fun x:τ. x⟧ : ⟦τ⟧ → ⟦τ⟧`, but
`fun x:τ. (fun y:τ. y) x ⇓̸τ->τ fun x:τ. x`. -/
theorem remark14 (τ : Ty) :
    denote (.lam (.app (.lam Term.v0) Term.v0) : Closed false (τ ⤳ τ)) =
        denote (.lam Term.v0 : Closed false (τ ⤳ τ)) ∧
      ¬ (.lam (.app (.lam Term.v0) Term.v0) : Closed false (τ ⤳ τ)) ⇓ .lam Term.v0 := sorry

/-! ## §7.1 Formal approximation relation -/

/-- Definition 27 (Formal approximation, p. 57): the relation `d ⊲τ t` between
`d ∈ ⟦τ⟧` and a closed term `t ∈ Pcf_τ`, by induction on `τ`. -/
abbrev definition27 (τ : Ty) : definition22 τ → Closed false τ → Prop := Approx τ

/-- Definition 28 (Parallel closed substitution, p. 57): `⊢ σ : Γ` maps each
variable `x` of `Γ` to a closed term `σ(x) ∈ Pcf_{Γ(x)}`. -/
abbrev definition28 (Γ : Ctx Base) : Type := Subst (sig false) Γ []

/-- Definition 29 (Formal approximation for substitution, p. 57):
`ρ ⊲Γ σ ⇔ ∀ x ∈ dom(Γ). ρ(x) ⊲Γ(x) σ(x)`. -/
abbrev definition29 (ρ : definition23 Γ) (s : definition28 Γ) : Prop := ApproxEnv ρ s

/-- Theorem 30 (Fundamental property of formal approximation, p. 57): if
`Γ ⊢ t : τ` and `ρ ⊲Γ σ`, then `⟦t⟧(ρ) ⊲τ t[σ]`. -/
theorem theorem30 (t : Term false Γ τ) {ρ : definition23 Γ} {s : definition28 Γ}
    (h : definition29 ρ s) : definition27 τ (denote t ρ) (t.subst s) := sorry

/-- Theorem 30 for `Γ = ⋅` (p. 57): `⟦t⟧ ⊲τ t` for every closed `t ∈ Pcf_τ`. -/
theorem theorem30_closed (t : Closed false τ) :
    definition27 τ ((model false).denoteClosed t) t := sorry

/-- Theorem 31 (Adequacy, p. 58): for a closed term `t` and a value `v` of ground
type `γ ∈ {nat, bool}`, `⟦t⟧ = ⟦v⟧ ∈ ⟦γ⟧ ⇒ t ⇓γ v`. -/
theorem theorem31 {γ : Ty} (hγ : γ.IsGround) {t v : Closed false γ} (hv : Value v)
    (h : (model false).denoteClosed t = (model false).denoteClosed v) : t ⇓ v := sorry

/-- Theorems 28 and 31 (pp. 53, 58): at ground type, `t ⇓γ v ⇔ ⟦t⟧ = ⟦v⟧` for
every value `v`. -/
theorem theorem31_iff {γ : Ty} (hγ : γ.IsGround) {t v : Closed false γ} (hv : Value v) :
    t ⇓ v ↔ (model false).denoteClosed t = (model false).denoteClosed v := sorry

/-! ## §7.2 Proof of the fundamental property of formal approximation -/

/-- Lemma 32 (p. 58): the least element approximates any program:
`⊥_⟦τ⟧ ⊲τ t` for every `t ∈ Pcf_τ`. -/
theorem lemma32 (t : Closed false τ) : definition27 τ ⊥ t := sorry

/-- Lemma 34 (p. 58): if `d' ⊑ d` and `d ⊲τ t` then `d' ⊲τ t`. -/
theorem lemma34 {d d' : definition22 τ} {t : Closed false τ} (hle : d' ≤ d)
    (h : definition27 τ d t) : definition27 τ d' t := sorry

/-- Theorem 30, proof, case Succ (p. 59), equation (9): `d' ⊲nat t'` implies
`succ⊥(d') ⊲nat succ(t')`. -/
theorem theorem30_succ {d' : Part ℕ} {t' : Closed false .nat} (h : definition27 .nat d' t') :
    definition27 .nat (Flat.map Nat.succ d') (Term.succ t') := sorry

/-- Theorem 30, proof, case If (p. 59), equation (10): if `d₁ ⊲bool t₁`,
`d₂ ⊲τ t₂` and `d₃ ⊲τ t₃`, then `if(d₁, d₂, d₃) ⊲τ if t₁ then t₂ else t₃`. -/
theorem theorem30_ite {d₁ : Part Bool} {d₂ d₃ : definition22 τ} {t₁ : Closed false .bool}
    {t₂ t₃ : Closed false τ} (h₁ : definition27 .bool d₁ t₁) (h₂ : definition27 τ d₂ t₂)
    (h₃ : definition27 τ d₃ t₃) : definition27 τ (condFun d₁ d₂ d₃) (Term.ite t₁ t₂ t₃) := sorry

/-- Theorem 30, proof, case Fix (p. 60): `d ⊲τ->τ f ⇒ fix(d) ⊲τ fix f`, by
Scott induction on `{e ∈ ⟦τ⟧ ∣ e ⊲τ fix f}`. -/
theorem theorem30_fix {d : definition22 (τ ⤳ τ)} {f : Closed false (τ ⤳ τ)}
    (h : definition27 (τ ⤳ τ) d f) : definition27 τ (fix (Model.fn d)) (Term.fix f) := sorry

/-! ## §7.3 Extensionality -/

/-- Definition 30 (Contextual preorder, p. 61): `Γ ⊢ t ≤ctx t' : τ` iff for all
contexts `⋅ ⊢_{Γ,τ} C : γ` with `γ` ground and all values `v`,
`C[t] ⇓γ v ⇒ C[t'] ⇓γ v`. As for Definition 21, `v` ranges over all closed
terms, which is equivalent since `⇓` only produces values. -/
theorem definition30 {t t' : Term false Γ τ} :
    t ≤ctx t' ↔ ∀ γ : Ty, γ.IsGround →
      ∀ (C : TypedLambda.Context (sig false) Γ τ [] γ) (v : Closed false γ),
        C.plug t ⇓ v → C.plug t' ⇓ v := sorry

/-- Lemma 36 (Monotony of formal approximation, p. 61): if
`t₁ ≤ctx t₂ : τ` then `d ⊲τ t₁ ⇒ d ⊲τ t₂`. -/
theorem lemma36 {t₁ t₂ : Closed false τ} (h : t₁ ≤ctx t₂) {d : definition22 τ}
    (hd : definition27 τ d t₁) : definition27 τ d t₂ := sorry

/-- Lemma 37 (Application contexts, p. 61): for closed `t₁, t₂ : τ`,
`t₁ ≤ctx t₂ : τ` iff for every `f : τ -> bool`,
`f t₁ ⇓bool true ⇒ f t₂ ⇓bool true`. -/
theorem lemma37 {t₁ t₂ : Closed false τ} :
    t₁ ≤ctx t₂ ↔ ∀ f : Closed false (τ ⤳ Ty.bool), .app f t₁ ⇓ .tt → .app f t₂ ⇓ .tt := sorry

/-- Lemma 37, proof (p. 62): the closed function `c = fun x:τ. C[x]` of a
closing context `C` has the results of `C[t]` at `t`. -/
theorem lemma37_close {γ : Ty} (C : TypedLambda.Context (sig false) [] τ [] γ)
    (t : Closed false τ) (v : Closed false γ) : .app C.close t ⇓ v ↔ C.plug t ⇓ v := sorry

/-- Lemma 37, proof (p. 62): a function testing "whether its argument is a
given boolean `true` or `false`": `(fun x:bool. eq_b(x)) t ⇓ true ⇔ t ⇓ b`. -/
theorem lemma37_testBool (b : Bool) (t : Closed false .bool) :
    .app (Term.testBool b) t ⇓ .tt ↔ t ⇓ Term.bool b := sorry

/-- Lemma 37, proof (p. 62): a function testing whether its argument is "a
given natural number `n`": `(fun x:nat. eqₙ(x)) t ⇓ true ⇔ t ⇓ n̄`. -/
theorem lemma37_testNum (n : ℕ) (t : Closed false .nat) :
    .app (Term.testNum n) t ⇓ .tt ↔ t ⇓ Term.num n := sorry

/-- Proposition 38 (Contextual preorder corresponds to formal approximation,
p. 62): for closed `t₁, t₂ ∈ Pcf_τ`, `t₁ ≤ctx t₂ : τ ⇔ ⟦t₁⟧ ⊲τ t₂`. -/
theorem proposition38 {t₁ t₂ : Closed false τ} :
    t₁ ≤ctx t₂ ↔ definition27 τ ((model false).denoteClosed t₁) t₂ := sorry

/-- Proposition 39 (Extensionality properties of contextual preorder, p. 62):
for `γ = bool` or `nat`, `t₁ ≤ctx t₂ : γ` iff `∀ v. (t₁ ⇓γ v ⇒ t₂ ⇓γ v)`; at a
function type `τ -> τ'`, `t₁ ≤ctx t₂ : τ -> τ'` iff
`∀ t ∈ Pcf_τ. (t₁ t ≤ctx t₂ t : τ')`. -/
theorem proposition39 :
    (∀ {γ : Ty}, γ.IsGround → ∀ {t₁ t₂ : Closed false γ},
      t₁ ≤ctx t₂ ↔ ∀ v, t₁ ⇓ v → t₂ ⇓ v) ∧
    ∀ {t₁ t₂ : Closed false (σ ⤳ τ)}, t₁ ≤ctx t₂ ↔ ∀ t : Closed false σ, .app t₁ t ≤ctx .app t₂ t := sorry

/-- Proposition 39 for contextual equivalence (p. 62): at ground types
`t₁ ≅ctx t₂ : γ` iff `∀ v. (t₁ ⇓γ v ⇔ t₂ ⇓γ v)`, and at function types
`t₁ ≅ctx t₂ : τ -> τ'` iff `∀ t ∈ Pcf_τ. (t₁ t ≅ctx t₂ t : τ')`. This is the
form used in the proof of Theorem 41. -/
theorem proposition39_ctxEquiv :
    (∀ {γ : Ty}, γ.IsGround → ∀ {t₁ t₂ : Closed false γ},
      t₁ ≅ctx t₂ ↔ ∀ v, t₁ ⇓ v ↔ t₂ ⇓ v) ∧
    ∀ {t₁ t₂ : Closed false (σ ⤳ τ)}, t₁ ≅ctx t₂ ↔ ∀ t : Closed false σ, .app t₁ t ≅ctx .app t₂ t := sorry

/-! ## §7.4 Exercises -/

/-- Exercise 16 (p. 63): Lemmas 33 and 35. -/
theorem exercise16 (t : Closed false τ) :
    ChainClosed {d | definition27 τ d t} ∧
      ∀ {d : definition22 τ} {t' : Closed false τ}, (∀ v, t ⇓ v → t' ⇓ v) →
        definition27 τ d t → definition27 τ d t' := sorry

/-- Exercise 17 (p. 63), equation (17): for closed `t₁, t₂ ∈ Pcf_τ`,
`∀ v. (t₁ ⇓τ v ⇔ t₂ ⇓τ v) ⇒ t₁ ≅ctx t₂ : τ`. As hinted, by Proposition 38 and
Lemma 35. -/
theorem exercise17 {t₁ t₂ : Closed false τ} (h : ∀ v, t₁ ⇓ v ↔ t₂ ⇓ v) : t₁ ≅ctx t₂ := sorry

/-- Exercise 18 (p. 63): β-conversion is valid up to contextual equivalence:
if `x:τ ⊢ t : τ'` and `⊢ u : τ` then `(fun x:τ. t) u ≅ctx t[u/x] : τ'`. -/
theorem exercise18 (t : Term false [σ] τ) (u : Closed false σ) :
    .app (.lam t) u ≅ctx t.subst1 u := sorry

/-- Exercise 19 (p. 63): the converse of (17) holds at ground types, but not at
function types: as hinted, `Ω_{nat -> nat} ≅ctx fun x:nat. Ω_nat`, yet the
second evaluates to itself while the first diverges. -/
theorem exercise19 :
    (∀ {γ : Ty}, γ.IsGround → ∀ {t₁ t₂ : Closed false γ}, t₁ ≅ctx t₂ →
      ∀ v, t₁ ⇓ v ↔ t₂ ⇓ v) ∧
    ((Term.omega (Ty.nat ⤳ Ty.nat) : Closed false _) ≅ctx .lam (Term.omega Ty.nat) ∧
      (.lam (Term.omega Ty.nat) : Closed false (Ty.nat ⤳ Ty.nat)) ⇓ .lam (Term.omega Ty.nat) ∧
      Diverges (Term.omega (Ty.nat ⤳ Ty.nat) : Closed false _)) ∧
    ¬ ∀ {t₁ t₂ : Closed false (Ty.nat ⤳ Ty.nat)}, t₁ ≅ctx t₂ → ∀ v, t₁ ⇓ v ↔ t₂ ⇓ v := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.Adequacy
