module

public import Silmaril.Calculi.PCF.Soundness
public import Silmaril.Calculi.PCF.Contextual
public import Silmaril.Semantics.Domain.ScottInduction

/-! # Adequacy

§7.1–7.2 of the notes, and Theorem 24 of §6.1, for PCF and for PCF+`por`.

The formal approximation relation `d ⊲τ t` (Definition 27) is a logical
relation between elements of `⟦τ⟧` and closed terms of type `τ`. It is closed
under `⊥`, lubs of chains and evaluation (Lemmas 32–35), and every term
approximates its own denotation (Theorem 30, the fundamental property). At
ground types this is adequacy (Theorem 31): `⟦t⟧ = ⟦v⟧` implies `t ⇓ v`. At
function types adequacy fails (Remark 14, `not_eval_of_denote_eq_arrow`).

The fundamental property is proved as in the notes, by induction on terms.
Each operator gets its own lemma about closed terms (the notes' equations (9)
and (10)); the case of `fix` is Scott induction. The flag `p` is arbitrary, so
the proof also covers `por`, for which the notes give no argument. Parallel or
preserves approximation because each of its defined results is justified by
one of the rules PorL, PorR and PorF.

Soundness, compositionality and adequacy together show that denotational
equality implies contextual equivalence (Theorem 24), for open terms too, and
that the denotational order implies the contextual preorder. That argument does
not depend on PCF: it is `TypedLambda.Observation.ctxEquiv_of_denote_eq` and
`TypedLambda.Observation.ctxLe_of_denote_le`, for any sound and adequate model, and
the PCF theorems instantiate it. The preorder version uses that the denotation
of a context is monotone in its hole (`TypedLambda.Context.denote_mono`).

Statements about closed terms use the element `⟦t⟧ = denote t Tuple.nil` of
`⟦τ⟧` (`Model.denoteClosed`, Remark 12) rather than the constant function on
the empty environment; `Model.denote_eq_iff_denoteClosed_eq` converts.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {p : Bool}

/-- Definition 27: the formal approximation relation `d ⊲τ t` between an
element `d ∈ ⟦τ⟧` and a closed term `t : τ`.

* `d ⊲nat t` iff `d = n` implies `t ⇓ n̄`;
* `d ⊲bool t` iff `d = b` implies `t ⇓ b`;
* `d ⊲τ->τ' t` iff `e ⊲τ u` implies `d(e) ⊲τ' t u`. -/
def Approx : (τ : Ty) → (model p).ty τ → Closed p τ → Prop
  | .base .nat, d, t => ∀ n : ℕ, (d : Part ℕ) = Part.some n → t ⇓ Term.num n
  | .base .bool, d, t => ∀ b : Bool, (d : Part Bool) = Part.some b → t ⇓ Term.bool b
  | .arrow σ τ, d, t => ∀ e u, Approx σ e u → Approx τ (Model.app d e) (.app t u)

/-- Definition 29: the formal approximation `ρ ⊲Γ s` between an environment and
a parallel closed substitution (Definition 28), componentwise. -/
def ApproxEnv {Γ : Ctx Base} (ρ : (model p).Env Γ) (s : Subst (sig p) Γ []) : Prop :=
  ∀ a x, Approx a (ρ a x) (s a x)

namespace Approx

/-- Lemma 32: `⊥` approximates every term. -/
theorem bot : (τ : Ty) → {t : Closed p τ} → Approx τ ⊥ t := sorry

/-- Lemma 33: `{d ∈ ⟦τ⟧ ∣ d ⊲τ t}` is chain-closed. At ground types every set
is chain-closed, since chains in flat domains are eventually constant. -/
theorem chainClosed : (τ : Ty) → (t : Closed p τ) → ChainClosed {d | Approx τ d t} := sorry

/-- Lemmas 32 and 33: `{d ∈ ⟦τ⟧ ∣ d ⊲τ t}` is admissible, so Scott induction
applies to it. -/
theorem admissible (τ : Ty) (t : Closed p τ) : Admissible {d | Approx τ d t} := sorry

/-- Lemma 35: if every result of `t` is a result of `t'`, then `t'`
approximates whatever `t` approximates. -/
theorem of_eval : (τ : Ty) → {d : (model p).ty τ} → {t t' : Closed p τ} →
    (∀ v, t ⇓ v → t' ⇓ v) → Approx τ d t → Approx τ d t' := sorry

end Approx

namespace ApproxEnv

variable {Γ : Ctx Base}

/-- Extending both sides: `ρ[x ↦ d] ⊲ σ[x ↦ u]`. -/
theorem cons {σ : Ty} {ρ : (model p).Env Γ} {s : Subst (sig p) Γ []} {d : (model p).ty σ}
    {u : Closed p σ} (hd : Approx σ d u) (h : ApproxEnv ρ s) : ApproxEnv (d ::ₜ ρ) (u ::ₜ s) := sorry

end ApproxEnv

/-! ### The operators preserve approximation

These are the cases of the proof of Theorem 30 for the operators, stated for
closed terms as in equations (9) and (10) of the notes. -/

section Operators

/-- Case Zero. -/
theorem approx_zero (args : Tuple (Closed p) []) :
    Approx .nat (Part.some 0 : Part ℕ) (.op .zero args) := sorry

/-- Case True. -/
theorem approx_tt (args : Tuple (Closed p) []) :
    Approx .bool (Part.some true : Part Bool) (.op .tt args) := sorry

/-- Case False. -/
theorem approx_ff (args : Tuple (Closed p) []) :
    Approx .bool (Part.some false : Part Bool) (.op .ff args) := sorry

/-- Case Succ, equation (9): `d ⊲ t` implies `succ⊥(d) ⊲ succ(t)`. -/
theorem approx_succ {args : Tuple (Closed p) [.nat]} {d : Part ℕ}
    (h : Approx .nat d args.head) : Approx .nat (Flat.map Nat.succ d) (.op .succ args) := sorry

/-- Case Pred. -/
theorem approx_pred {args : Tuple (Closed p) [.nat]} {d : Part ℕ}
    (h : Approx .nat d args.head) : Approx .nat (Flat.lift predPart d) (.op .pred args) := sorry

/-- Case IsZ. -/
theorem approx_isZero {args : Tuple (Closed p) [.nat]} {d : Part ℕ}
    (h : Approx .nat d args.head) :
    Approx .bool (Flat.map (fun n : ℕ => n == 0) d) (.op .isZero args) := sorry

/-- Case If, equation (10): if `d₁ ⊲ t₁`, `d₂ ⊲ t₂` and `d₃ ⊲ t₃` then
`if(d₁, d₂, d₃) ⊲ if t₁ then t₂ else t₃`. -/
theorem approx_ite {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} {d₁ : Part Bool}
    {d₂ d₃ : (model p).ty τ} (h₁ : Approx .bool d₁ args.head)
    (h₂ : Approx τ d₂ args.tail.head) (h₃ : Approx τ d₃ args.tail.tail.head) :
    Approx τ (condFun d₁ d₂ d₃) (.op (.ite τ) args) := sorry

/-- Case Fix: `d ⊲ f` implies `fix(d) ⊲ fix(f)`, by Scott induction on
`{e ∈ ⟦τ⟧ ∣ e ⊲τ fix(f)}`. -/
theorem approx_fix {τ : Ty} {args : Tuple (Closed p) [τ ⤳ τ]} {d : (model p).ty (τ ⤳ τ)}
    (h : Approx (τ ⤳ τ) d args.head) : Approx τ (fix (Model.fn d)) (.op (.fix τ) args) := sorry

/-- Case Por: parallel or preserves approximation. -/
theorem approx_por {args : Tuple (Closed true) [.bool, .bool]} {d₁ d₂ : Part Bool}
    (h₁ : Approx .bool d₁ args.head) (h₂ : Approx .bool d₂ args.tail.head) :
    Approx .bool (porFun d₁ d₂) (.op .por args) := sorry

end Operators

/-- The operator cases of Theorem 30: if the arguments of an operator satisfy
the fundamental property then so does the operator. -/
theorem approx_denote_subst_op {Γ : Ctx Base} {as : List Ty} {τ : Ty} (o : Op p as τ)
    (args : Tuple (Term p Γ) as) {ρ : (model p).Env Γ} {s : Subst (sig p) Γ []}
    (ih : ∀ a x, Approx a (denote (args a x) ρ) ((args a x).subst s)) :
    Approx τ (denote (.op o args) ρ) ((TypedLambda.Term.op o args).subst s) := sorry

/-! ### Adequacy -/

/-! ### Theorem 24 -/

/-- For closed terms: if `⟦t₁⟧ ⊑ ⟦t₂⟧` in `⟦τ⟧` then `t₁ ≤ctx t₂`. -/
theorem CtxLe.of_denoteClosed_le {τ : Ty} {t₁ t₂ : Closed p τ}
    (h : (model p).denoteClosed t₁ ≤ (model p).denoteClosed t₂) : t₁ ≤ctx t₂ := sorry

end Silmaril.Calculi.PCF
