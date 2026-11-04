module

public import Silmaril.Calculi.PCF.Partrec
public import Silmaril.Calculi.PCF.CallByValue
public import Silmaril.Calculi.PCF.SmallStep
public import Silmaril.Calculi.PCF.Contextual


/-! # §5 PCF (pp. 37–46)

Index of §5 of the notes against `Silmaril.Calculi.TypedLambda` and `Silmaril.Calculi.PCF`. PCF is
`Silmaril.Calculi.PCF.Term false`; the same development covers PCF+`por`
(`Term true`, §8), so the general theorems are stated for an arbitrary flag
and the wrappers below specialize them to PCF.

Terms are intrinsically typed de Bruijn terms: `Term false Γ τ` is the set
`Pcf_{Γ,τ}` of Definition 17, α-equivalent terms are equal, and substitution
preserves types by construction. The notes' raw terms with the typing
relation of Fig. 2 are `Silmaril.Calculi.TypedLambda.Raw` with `Raw.HasType`, and
`Raw.equivTerm` identifies the two presentations. Variables are positions in
the context, so contexts are lists of types (Remark 5).

Evaluation contexts (Definition 19) may put the hole under any operator,
including `fix`, as in the notes' grammar, and under binders, where variables
of the plugged term may be captured.

Application is call by name, so the programs of Example 19 and Exercise 13
may receive arguments that are not yet numerals. The general development
(`Silmaril.Calculi.PCF.Partrec`, `Silmaril.Calculi.PCF.Examples`) proves them correct for any
closed arguments that evaluate to numerals, and the wrappers specialize to
numerals.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.Pcf

open Silmaril.Calculi

open Silmaril.Calculi.PCF
open Silmaril.Calculi.TypedLambda (Ctx Var Raw)
open scoped Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {Γ : Ctx Base} {σ τ : Ty}

/-! ## §5.1 Syntax -/

/-- Figure 1 (p. 37): the types `τ ::= nat ∣ bool ∣ τ -> τ`, CSLib's simple
types over the base types `nat` and `bool`. -/
abbrev figure1 : Type := Ty

/-- Definition 16 (Contexts, p. 39): a typing context. With de Bruijn indices,
variables are positions, and a context is the list of their types
(Remark 5's view). The empty context `⋅` is `[]` and `Γ, x:τ` is `τ :: Γ`. -/
abbrev definition16 : Type := Ctx Base

/-- Remark 5 (p. 39): contexts as finite lists. -/
theorem remark5 : definition16 = List Ty := sorry

/-- Figure 2 (p. 40): the typing relation `Γ ⊢ t : τ` on raw terms. -/
abbrev figure2 (Γ : Ctx Base) (t : Raw (sig false)) (τ : Ty) : Prop := Raw.HasType Γ t τ

/-- Definition 17 (p. 39): `Pcf_{Γ,τ} = {t ∣ Γ ⊢ t : τ}`, the terms of type `τ`
in context `Γ`, and `Pcf_τ` for closed terms. -/
abbrev definition17 (Γ : Ctx Base) (τ : Ty) : Type := Term false Γ τ

/-- Definition 17: intrinsic terms are exactly the well-typed raw terms. -/
def definition17_equiv (Γ : Ctx Base) (τ : Ty) :
    {t : Raw (sig false) // Raw.HasType Γ t τ} ≃ definition17 Γ τ :=
  Raw.equivTerm (S := sig false) Γ τ

/-- Proposition 22 (Typing is stable by substitution, p. 39): if `Γ ⊢ t : τ`
and `Γ, x:τ ⊢ t' : τ'` then `Γ ⊢ t'[t/x] : τ'`. For intrinsic terms this is the
type of `Term.subst1`. -/
theorem proposition22 {t t' : Raw (sig false)} {τ' : Ty} (ht : Raw.HasType Γ t τ)
    (ht' : Raw.HasType (τ :: Γ) t' τ') : Raw.HasType Γ (Raw.subst t' 0 t) τ' := sorry

/-! ## §5.2 Operational semantics -/

/-- Figure 3 (p. 41): the values `v ::= 𝟶 ∣ succ(v) ∣ true ∣ false ∣ fun x:τ. t`;
at `nat` they are the numerals `n̄`. -/
theorem figure3_values {v : Closed false .nat} {b : Closed false .bool}
    {f : Closed false (σ ⤳ τ)} :
    (Value v → ∃ n, v = Term.num n) ∧ (Value b → ∃ c, b = Term.bool c) ∧
      (Value f → ∃ t, f = .lam t) := sorry

/-- Figure 3 (p. 41): the evaluation rules `t ⇓τ v`. -/
theorem figure3 {t t' u v : Closed false τ} {n m : Closed false .nat} {b : Closed false .bool}
    {g : Closed false (σ ⤳ τ)} {g' : Term false [σ] τ} {a : Closed false σ}
    {k : Closed false (τ ⤳ τ)} :
    (Value v → v ⇓ v) ∧ (n ⇓ m → .succ n ⇓ .succ m) ∧ (n ⇓ .succ m → .pred n ⇓ m) ∧
      (n ⇓ .zero → .isZero n ⇓ .tt) ∧ (n ⇓ .succ m → .isZero n ⇓ .ff) ∧
      (b ⇓ .tt → t ⇓ v → .ite b t t' ⇓ v) ∧ (b ⇓ .ff → t' ⇓ v → .ite b t t' ⇓ v) ∧
      (g ⇓ .lam g' → g'.subst1 a ⇓ u → .app g a ⇓ u) ∧
      (.app k (Term.fix k) ⇓ v → Term.fix k ⇓ v) := sorry

/-- Example 18 (The diverging term, p. 40): `Ωτ = fix(fun x:τ. x)` diverges,
`Ωτ ⇑τ`. -/
theorem example18 (v : Closed false τ) : ¬ Term.omega τ ⇓ v := sorry

/-- Remark 6 (Call-by-name and call-by-value, p. 42): `(fun x:nat. 𝟶) Ω_nat`
returns `𝟶` in call-by-name but diverges in call-by-value (rule Fun-CBV). -/
theorem remark6 : constZeroOmega false ⇓ .zero ∧ ∀ v, ¬ EvalV (constZeroOmega false) v := sorry

/-- Remark 7 (Small and big step semantics, p. 43): with the transitions `⇝`
of Figure 4, `t ⇓τ v ⇔ t ⇝⋆τ v` for every value `v`. -/
theorem remark7 {t v : Closed false τ} (hv : Value v) :
    t ⇓ v ↔ Relation.ReflTransGen Step t v := sorry

/-- Example 19 (Partial recursive functions in PCF, p. 43): defined results
of every partial recursive `φ : ℕ ⇀ ℕ` are simulated by a PCF term
`φ' : nat -> nat` such that for all `n ∈ ℕ`, if `φ(n)` is defined then
`φ' n̄ ⇓nat φ(n)`. The statement gives the forward implication; it does not
constrain evaluation at inputs where `φ` is undefined. -/
theorem example19 {φ : ℕ →. ℕ} (hφ : Partrec φ) :
    ∃ φ' : Closed false (Ty.nat ⤳ Ty.nat), ∀ n, ∀ y ∈ φ n, .app φ' (Term.num n) ⇓ Term.num y := sorry

/-- Example 19 (p. 43), for partial recursive functions `f : ℕⁿ ⇀ ℕ` of several
arguments, coded curried by `t : nat -> ⋯ -> nat -> nat`: if `f(v)` is
defined then `t v̄₀ ⋯ v̄ₙ₋₁ ⇓nat f(v)`. -/
theorem example19_vector {n : ℕ} {f : List.Vector ℕ n →. ℕ} (hf : Nat.Partrec' f) :
    ∃ t : Closed false (Ty.arrows n), ∀ v, ∀ y ∈ f v,
      Term.apps t (fun i => Term.num (v.get i)) ⇓ Term.num y := sorry

/-- Example 19 (p. 44): if `f'` codes `f` and `g'` codes `g`, then
`H = fix(fun h. fun y x. if zero?(y) then f' x else g' (pred(y)) (h (pred(y)) x) x)`
codes the function `h` defined by primitive recursion, `h(0, x) = f(x)` and
`h(y + 1, x) = g(y, h(y, x), x)`. The recursion variable comes first, as in
Mathlib's `Nat.Primrec'`, and the arguments `x` are any closed terms. -/
theorem example19_prec {n : ℕ} {f' : Closed false (Ty.arrows n)}
    {g' : Closed false (Ty.arrows (n + 2))} {x : Fin n → Closed false .nat} (h : ℕ → ℕ)
    (h0 : Term.apps f' x ⇓ Term.num (h 0))
    (hs : ∀ k {u w : Closed false .nat}, u ⇓ Term.num k → w ⇓ Term.num (h k) →
      Term.apps (.app (.app g' u) w) x ⇓ Term.num (h (k + 1)))
    (y : ℕ) : Term.apps (.app (Realizes.precTerm f' g') (Term.num y)) x ⇓ Term.num (h y) := sorry

/-! ## §5.3 Contextual equivalence

Definition 18 (Contextual equivalence – informal, p. 45) is made precise by
Definition 21: complete programs are closed terms of type `nat` or `bool`
(`Ty.IsGround`), and the observable results are the values they evaluate to. -/

/-- Definition 19 (Evaluation contexts, p. 45): terms with a hole, and `C[t]`. -/
abbrev definition19 (Δ : Ctx Base) (σ : Ty) (Γ : Ctx Base) (τ : Ty) : Type :=
  TypedLambda.Context (sig false) Δ σ Γ τ

/-- Definition 20 (Typing for evaluation contexts, p. 45): the judgement
`Γ ⊢_{Δ,σ} C : τ` is `C : definition20 Δ σ Γ τ`. Contexts are intrinsically
typed, so the typed contexts of Definition 20 are the contexts of
Definition 19. -/
abbrev definition20 (Δ : Ctx Base) (σ : Ty) (Γ : Ctx Base) (τ : Ty) : Type :=
  definition19 Δ σ Γ τ

/-- Definition 21 (Contextual equivalence, p. 46): `Γ ⊢ t ≅ctx t' : τ` iff for
all contexts `⋅ ⊢_{Γ,τ} C : γ` with `γ` ground and all values `v`,
`C[t] ⇓γ v ⇔ C[t'] ⇓γ v`. The statement quantifies over all closed `v : γ`
rather than values only; this is equivalent, since `⇓` only produces values
(`Eval.value`). -/
theorem definition21 {t t' : Term false Γ τ} :
    t ≅ctx t' ↔ ∀ γ : Ty, γ.IsGround → ∀ (C : definition20 Γ τ [] γ) (v : Closed false γ),
      (C.plug t ⇓ v ↔ C.plug t' ⇓ v) := sorry

/-- Remark 8 (p. 46): contextually equivalent programs diverge together. The
notes state it for any `Γ ⊢ t ≅ctx t' : τ`; divergence is only defined for
closed terms, and the argument takes the context `C = −`, which is a complete
program only at ground type, so it is stated here for closed terms of ground
type. -/
theorem remark8 {γ : Ty} (hγ : γ.IsGround) {t t' : Closed false γ} (h : t ≅ctx t') :
    Diverges t ↔ Diverges t' := sorry

/-! ## §5.4 Exercises -/

/-- Exercise 11 (p. 46): evaluation is deterministic (Proposition 23). -/
theorem exercise11 {t v v' : Closed false τ} (h : t ⇓ v) (h' : t ⇓ v') : v = v' := sorry

/-- Exercise 12 (p. 46): there are no PCF types `τ₁, τ₂, τ₃` with
`⋅ ⊢ fun f:τ₁. (fun x:τ₂. f (x x)) (fun x:τ₂. f (x x)) : τ₃`. -/
theorem exercise12 (τ₁ τ₂ τ₃ : Ty) : ¬ Raw.HasType [] (Raw.churchY (S := sig false) τ₁ τ₂) τ₃ := sorry

/-- Exercise 13 (p. 46): for all `m, n ∈ ℕ`, `plus m̄ n̄ ⇓nat m + n` and
`mul m̄ n̄ ⇓nat m ⋅ n`; and the factorial function
`fact = fix(fun (f:nat -> nat) (y:nat). if zero?(y) then 1 else mul y (f pred(y)))`
computes the factorial, `fact n̄ ⇓nat n!`. -/
theorem exercise13 (m n : ℕ) :
    .app (.app plus (Term.num m)) (Term.num n) ⇓ (Term.num (m + n) : Closed false .nat) ∧
      .app (.app mul (Term.num m)) (Term.num n) ⇓ (Term.num (m * n) : Closed false .nat) ∧
      .app fact (Term.num n) ⇓ (Term.num n.factorial : Closed false .nat) := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.Pcf
