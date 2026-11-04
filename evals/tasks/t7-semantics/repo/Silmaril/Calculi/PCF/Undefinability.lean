module

public import Silmaril.Calculi.TypedLambda.LogicalRelation
public import Silmaril.Calculi.PCF.Por

/-! # Undefinability of parallel or

No closed PCF term denotes parallel or (`por_undefinable`; Theorem 40 of the
notes, whose proof they omit, pointing to Plotkin's activity lemma, to stable
functions, and to logical relations). We follow the logical-relation proof
(Sieber), with Sieber's ternary relation

  `R(d₀, d₁, d₂) :⇔ d₀ = ⊥ ∨ d₁ = ⊥ ∨ d₀ = d₁ = d₂`

at both ground types, extended to all types as a logical relation
(`Silmaril.Calculi.TypedLambda.SieberLogRel` over the model of PCF). Every PCF operator
preserves it (`Sieber.op`):

* constants trivially, since their three components are equal;
* `succ`, `pred` and `zero?` because they are strict (`SieberRel.map`);
* `if` because the relation holds at every type of all triples whose first or
  second component is `⊥` (`SieberLogRel.condFun_mem`);
* `fix` by Scott induction, because the relation is admissible at every type
  (`SieberLogRel.admissible`, `LogRel.fix_mem`).

By the fundamental lemma, the denotation `d` of every closed PCF term
satisfies `R(d, d, d)` (`Sieber.denoteClosed_mem`). Parallel or does not: it
sends the related triples `(true, ⊥, false)` and `(⊥, true, false)` to the
unrelated triple `(true, true, false)`. The proof is purely denotational; it
does not need adequacy.

With the characterization of parallel or by three table entries (`PorSpec`),
the denotation of a closed PCF term `t : bool -> bool -> bool` fails one of
them (`not_porSpec_denoteClosed`).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- Sieber's logical relation on the domain model of PCF. -/
abbrev Sieber : (τ : Ty) → (Fin 3 → (model false).ty τ) → Prop :=
  SieberLogRel (model false)

namespace Sieber

end Sieber

end Silmaril.Calculi.PCF
