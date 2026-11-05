module

public import Silmaril.Calculi.TypedLambda.LogicalRelation
public import Silmaril.Semantics.Domain.Stable

/-! # The stability logical relation

Berry's stability (`Silmaril.Semantics.Domain.Stable`) as a logical relation over any
domain model. At every base type we take the ternary meet relation
`Silmaril.Semantics.Domain.MeetRel`: `d₀` and `d₁` have a common upper bound and `d₂`
is their greatest lower bound. The logical relation `MeetLogRel M` extends it
to all types. By the fundamental lemma (`LogRel.fundamental`), every term of
a signature whose operators preserve the relation denotes a family of
elements that preserve it; at a first-order type such as
`bool -> bool -> bool` over flat base domains, this says the denotation is a
stable function (`Silmaril.Calculi.PCF.stable_denoteClosed_bool₂`).

As with Sieber's relation (`Silmaril.Calculi.TypedLambda.SieberLogRel`), the triples
`(⊥, x, ⊥)` and `(x, ⊥, ⊥)` are related at every type (`of_bot_left`,
`of_bot_right`), which is what makes the conditional, into any type, preserve
the relation (`condFun_mem`). Admissibility (needed for `fix`) is checked at
base types; on flat domains it holds by `Silmaril.Semantics.Domain.Flat.meetRel_admissible`.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open OmegaCompletePartialOrder Silmaril.Semantics.Domain Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {S : Signature} {M : Model S}

/-- The stability logical relation over a model: the meet relation `MeetRel`
at every base type, extended to all types. -/
abbrev MeetLogRel (M : Model S) : (τ : Ty S.Base) → (Fin 3 → M.ty τ) → Prop :=
  LogRel M fun _ d => MeetRel d

namespace MeetLogRel

end MeetLogRel

end Silmaril.Calculi.TypedLambda
