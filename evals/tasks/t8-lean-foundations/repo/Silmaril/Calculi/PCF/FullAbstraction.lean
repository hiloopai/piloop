module

public import Silmaril.Calculi.PCF.Extensionality
public import Silmaril.Calculi.PCF.Undefinability
public import Silmaril.Calculi.PCF.Adequacy
public import Silmaril.Calculi.PCF.Density
public import Silmaril.Calculi.TypedLambda.FullAbstraction

/-! # Full abstraction

A denotational model is *fully abstract* when contextually equivalent terms
have equal denotations (`FullyAbstract`); the converse holds for the PCF model
by adequacy. The domain model of PCF is not fully abstract. The witnesses are
the terms

```
T_b = fun f : bool -> bool -> bool.
        if (f true Ω) then
          if (f Ω true) then
            if (f false false) then Ω else b
          else Ω
        else Ω
```

(`Term.tcase b`), which test the three entries of the table of parallel or
that characterize it (`PorSpec`). Denotationally they differ, since
`⟦T_b⟧(por) = b` (`app_denote_tcase_porElem`, `denote_tcase_ne`). Yet
`⟦T_b⟧` sends the denotation of every closed PCF term to `⊥`
(`app_denote_tcase_denoteClosed`), since otherwise that denotation would
satisfy `PorSpec` and be parallel or, contradicting `por_undefinable`. By
soundness, `T_b t` then diverges for every closed PCF term `t`
(`tcase_app_diverges`), so by extensionality (`ctxEquiv_arrow_iff`,
`ctxEquiv_iff_eval`) `T_true ≅ctx T_false` in PCF (`tcase_ctxEquiv`), and the
model of PCF is not fully abstract (`not_fullyAbstract`).

In PCF+`por` the two terms are distinguished by the context `− por`, since
`T_b (fun x. fun y. por(x, y)) ⇓ b` (`eval_tcase_app_porLam`). So the
inclusion of PCF into PCF+`por`, which reflects contextual equivalence
(`CtxEquiv.of_embed`), does not preserve it (`not_ctxEquiv_embed_tcase`).

## Full abstraction for PCF+`por`

Adding `por` repairs this: the model of PCF+`por` is fully abstract
(`fullyAbstract_por`, Plotkin). Every element of `⟦τ⟧` is the supremum of an
ω-chain of definable elements (`exists_chain_definable`), and at base type the
order reflects evaluation (`denoteClosed_le_of_eval_imp`, by adequacy). The
generic argument `Observation.denote_le_of_ctxLe` then turns `t₁ ≤ctx t₂` into
`⟦t₁⟧ ⊑ ⟦t₂⟧` (`denote_le_of_ctxLe`); with soundness and adequacy this gives
`ctxLe_iff_denote_le` and `ctxEquiv_iff_denote_eq`.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The domain model of PCF (`p = false`) or of PCF+`por` (`p = true`) is
fully abstract when contextually equivalent terms have equal denotations. -/
def FullyAbstract (p : Bool) : Prop :=
  ∀ {Γ : Ctx Base} {τ : Ty} (t₁ t₂ : Term p Γ τ), t₁ ≅ctx t₂ → denote t₁ = denote t₂

section Tcase

variable {p : Bool} {Γ : Ctx Base}

/-- The body of `T_b`, with `f` the innermost variable. -/
def Term.tcaseBody (b : Bool) : Term p (Ty.bool₂ :: Γ) Ty.bool :=
  let call (x y : Term p (Ty.bool₂ :: Γ) Ty.bool) := TypedLambda.Term.app (.app Term.v0 x) y
  Term.ite (call Term.tt (Term.omega _))
    (Term.ite (call (Term.omega _) Term.tt)
      (Term.ite (call Term.ff Term.ff) (Term.omega _) (Term.bool b))
      (Term.omega _))
    (Term.omega _)

/-- The term `T_b = fun f. if (f true Ω) then if (f Ω true) then
if (f false false) then Ω else b else Ω else Ω`. -/
def Term.tcase (b : Bool) : Term p Γ (Ty.bool₂ ⤳ Ty.bool) :=
  .lam (Term.tcaseBody b)

/-- The function denoted by `T_b`: it tests the three entries of `PorSpec`. -/
noncomputable def tcaseFun (b : Bool) (f : (model p).ty Ty.bool₂) : Part Bool :=
  condFun (app₂ f (.some true) .none)
    (condFun (app₂ f .none (.some true))
      (condFun (app₂ f (.some false) (.some false)) .none (.some b))
      .none)
    .none

/-! ### `T_true ≅ctx T_false` in PCF -/

/-! ### `T_true` and `T_false` in PCF+`por` -/

/-- `T_b (fun x. fun y. por(x, y)) ⇓ b` in PCF+`por`. -/
theorem eval_tcase_app_porLam (b : Bool) :
    .app (Term.tcase b : Closed true _) Term.porLam ⇓ Term.bool b := sorry

/-- `T_true` and `T_false` are not contextually equivalent in PCF+`por`. -/
theorem not_ctxEquiv_tcase_por :
    ¬ (Term.tcase true : Closed true (Ty.bool₂ ⤳ Ty.bool)) ≅ctx Term.tcase false := sorry

/-- The inclusion of PCF into PCF+`por` reflects but does not preserve
contextual equivalence. -/
theorem exists_ctxEquiv_not_embed :
    ∃ t₁ t₂ : Closed false (Ty.bool₂ ⤳ Ty.bool), t₁ ≅ctx t₂ ∧ ¬ t₁.embed ≅ctx t₂.embed := sorry

end Tcase

section Por

end Por

end Silmaril.Calculi.PCF
