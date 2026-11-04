module

public import Silmaril.Calculi.PCF.Adequacy

/-! # Extensionality

§7.3 of the notes, for PCF and for PCF+`por`. Formal approximation is a
one-sided form of contextual equivalence: for closed terms,
`t₁ ≤ctx t₂ ⇔ ⟦t₁⟧ ⊲ t₂` (`ctxLe_iff_approx`). Two lemmas lead there:

* the contextual preorder preserves approximation (`Approx.of_ctxLe`);
* the contextual preorder between closed terms is determined by application
  contexts `f −` with `f : τ -> bool` (`ctxLe_iff_app`).

For the second, as in the notes, a closing context `C : γ` with a result `v`
becomes the closed function `f = fun x:τ. g C[x]`, where the closed test
`g : γ -> bool` returns `true` exactly on terms evaluating to `v`
(`exists_test`). The function is `TypedLambda.Context.close` applied to the context
`g C` (`Eval.app_close_iff`). The notes obtain `g` from Turing completeness; a
conditional suffices for booleans and a recursion on the numeral with `zero?`
and `pred` for numbers (`Term.testBool`, `Term.testNum`). The tests are built
from term formers `Term.eqBool`, `Term.eqNum` that can be applied in any
context.

The correspondence transfers extensionality of the domain order to the
contextual preorder (`ctxLe_iff_eval`, `ctxLe_arrow_iff`, and their
contextual-equivalence forms): at ground types `≤ctx` is inclusion of results,
and at function types it is tested pointwise on closed arguments. Terms with
the same results are contextually equivalent (`CtxEquiv.of_eval_iff`), so
β-conversion holds up to `≅ctx` (`ctxEquiv_beta`). The converse holds at ground
types only: `Ω_{nat -> nat} ≅ctx fun x:nat. Ω_nat`, but the second is a value
and the first diverges (`ctxEquiv_omega_lam_omega`).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {p : Bool}

namespace Eval

end Eval

/-! ### Testing for a ground value -/

section Tests

variable {Γ : Ctx Base}

/-- The closed test `fun x:nat. eqₙ(x)` for the numeral `n̄`. -/
def Term.testNum (n : ℕ) : Closed p (Ty.nat ⤳ Ty.bool) := .lam (Term.eqNum n Term.v0)

/-- The closed test `fun x:bool. eq_b(x)` for the boolean `b`. -/
def Term.testBool (b : Bool) : Closed p (Ty.bool ⤳ Ty.bool) := .lam (Term.eqBool b Term.v0)

end Tests

/-! ### Formal approximation and the contextual preorder -/

section Preorder

variable {τ : Ty}

end Preorder

/-! ### Contextual equivalence -/

section Equivalence

variable {τ : Ty}

/-- For closed terms, `t₁ ≅ctx t₂` iff `⟦t₁⟧ ⊲ t₂` and `⟦t₂⟧ ⊲ t₁`. -/
theorem ctxEquiv_iff_approx {t₁ t₂ : Closed p τ} :
    t₁ ≅ctx t₂ ↔ Approx τ (denoteClosed t₁) t₂ ∧ Approx τ (denoteClosed t₂) t₁ := sorry

end Equivalence

end Silmaril.Calculi.PCF
