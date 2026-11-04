module

public import Silmaril.Calculi.TypedLambda.Context
public import Silmaril.Calculi.TypedLambda.Denotation

/-! # Full abstraction from density and ground reflection

The standard argument (Plotkin 1977) that a model is fully abstract once its
definable elements are dense and its order reflects ground evaluation: for any
signature, observation and model,

* if every base type is ground,
* if at base types `⟦t⟧ ⊑ ⟦u⟧` follows from `t` evaluating only to results `u`
  evaluates to (ground reflection, a consequence of soundness and adequacy),
* and if every element of every `⟦σ⟧` is the least upper bound of an ω-chain of
  denotations of closed terms (density of definable elements),

then `t ≤ctx u` implies `⟦t⟧ ⊑ ⟦u⟧`, and `t ≅ctx u` implies `⟦t⟧ = ⟦u⟧`, for
open terms too. Together with the converse (`Observation.ctxLe_of_denote_le`)
this is full abstraction. Theorem 42 of the Cambridge notes (full abstraction
of the model of PCF+por) is its instance for PCF+por.

The closed case goes by induction on the type: at an arrow type, `⟦t⟧ d` is the
supremum of `⟦t⟧ ⟦uₙ⟧ = ⟦t uₙ⟧` over closed terms `uₙ` approximating `d`, and the
application context `− uₙ` preserves the contextual preorder. Open terms reduce
to closed ones by λ-abstracting their free variables.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open OmegaCompletePartialOrder Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {S : Signature}

namespace Observation

variable {O : Observation S} {M : Model S}

/-- Full abstraction for closed terms: if base types are ground, the model
reflects ground evaluation and definable elements are dense, then `t ≤ctx u`
implies `⟦t⟧ ⊑ ⟦u⟧`. -/
theorem denoteClosed_le_of_ctxLe
    (hbase : ∀ b, O.Ground (.base b))
    (hground : ∀ b {t u : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval u v) →
      M.denoteClosed t ≤ M.denoteClosed u)
    (hdense : ∀ σ (d : M.ty σ), ∃ c : Chain (M.ty σ), ωSup c = d ∧
      ∀ n, ∃ u : Term S [] σ, M.denoteClosed u = c n) :
    ∀ τ {t u : Term S [] τ}, O.CtxLe t u → M.denoteClosed t ≤ M.denoteClosed u
  | .base b, _, _, h => hground b fun v => h (.base b) (hbase b) .hole v
  | .arrow σ τ, t, u, h => by
      intro d
      obtain ⟨c, rfl, hc⟩ := hdense σ d
      change Model.app (M.denoteClosed t) (ωSup c) ≤ Model.app (M.denoteClosed u) (ωSup c)
      refine ((M.fn (M.denoteClosed t)).continuous c).le.trans (ωSup_le _ _ fun n => ?_)
      obtain ⟨v, hv⟩ := hc n
      have := denoteClosed_le_of_ctxLe hbase hground hdense τ
        (h.plug (Context.appL .hole v))
      simp only [Context.plug, Model.denote_app, hv] at this
      exact this.trans ((M.fn (M.denoteClosed u)).monotone (le_ωSup c n))

/-- Full abstraction for open terms: under the hypotheses of
`denoteClosed_le_of_ctxLe`, `Γ ⊢ t ≤ctx u` implies `⟦t⟧ ⊑ ⟦u⟧`. -/
theorem denote_le_of_ctxLe
    (hbase : ∀ b, O.Ground (.base b))
    (hground : ∀ b {t u : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval u v) →
      M.denoteClosed t ≤ M.denoteClosed u)
    (hdense : ∀ σ (d : M.ty σ), ∃ c : Chain (M.ty σ), ωSup c = d ∧
      ∀ n, ∃ u : Term S [] σ, M.denoteClosed u = c n) :
    ∀ {Γ : Ctx S.Base} {τ : Ty S.Base} {t u : Term S Γ τ}, O.CtxLe t u →
      M.denote t ≤ M.denote u
  | [], τ, _, _, h =>
      Model.denote_le_iff_denoteClosed_le.2 (denoteClosed_le_of_ctxLe hbase hground hdense τ h)
  | _ :: _, _, t, u, h => fun ρ => by
      have hlam := denote_le_of_ctxLe hbase hground hdense (h.plug (Context.lam .hole))
      rw [Tuple.eq_cons ρ]
      exact hlam ρ.tail ρ.head

/-- Full abstraction for contextual equivalence: under the hypotheses of
`denoteClosed_le_of_ctxLe`, `Γ ⊢ t ≅ctx u` implies `⟦t⟧ = ⟦u⟧`. -/
theorem denote_eq_of_ctxEquiv
    (hbase : ∀ b, O.Ground (.base b))
    (hground : ∀ b {t u : Term S [] (.base b)}, (∀ v, O.Eval t v → O.Eval u v) →
      M.denoteClosed t ≤ M.denoteClosed u)
    (hdense : ∀ σ (d : M.ty σ), ∃ c : Chain (M.ty σ), ωSup c = d ∧
      ∀ n, ∃ u : Term S [] σ, M.denoteClosed u = c n)
    {Γ : Ctx S.Base} {τ : Ty S.Base} {t u : Term S Γ τ} (h : O.CtxEquiv t u) :
    M.denote t = M.denote u :=
  le_antisymm (denote_le_of_ctxLe hbase hground hdense (ctxEquiv_iff_le.1 h).1)
    (denote_le_of_ctxLe hbase hground hdense (ctxEquiv_iff_le.1 h).2)

end Observation

end Silmaril.Calculi.TypedLambda
