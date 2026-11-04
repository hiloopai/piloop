module

public import Silmaril.Calculi.TypedLambda.Stability
public import Silmaril.Calculi.PCF.Por

/-! # Stability of PCF

Berry's stable functions (§8.2 of the notes; `Silmaril.Semantics.Domain.Stable`) on the
domain model of PCF. Parallel or is not stable (`not_stable_porFun`): the
pairs `(true, ⊥)` and `(⊥, true)` are bounded by `(true, true)` and have meet
`(⊥, ⊥)`, but `por(⊥, ⊥) = ⊥` is not the meet `true` of `por(true, ⊥)` and
`por(⊥, true)`. The left sequential or is stable (`stable_orFun`), being
built from the stable conditional (`Silmaril.Semantics.Domain.stable_condFun`).

More generally, the first-order denotation of every closed PCF term
`t : bool -> bool -> bool` is stable (`stable_denoteClosed_bool₂`). The proof
is a logical relation (`Silmaril.Calculi.TypedLambda.MeetLogRel` over the model of PCF,
here `StableRel`), preserved by every PCF operator (`StableRel.op`):

* constants, since constant triples are related (`MeetRel.const`);
* `succ`, `pred` and `zero?`, since flat liftings are stable
  (`Flat.stable_lift`, `Flat.stable_map`);
* `if`, into any type (`MeetLogRel.condFun_mem`);
* `fix`, by Scott induction, since the meet relation on flat domains is
  admissible (`Flat.meetRel_admissible`).

The fundamental lemma then gives the relation for every closed term at every
type (`StableRel.denoteClosed_mem`), which at `bool -> bool -> bool` is exactly
stability of the uncurried function `𝔹⊥ × 𝔹⊥ → 𝔹⊥`. This gives a second
proof that parallel or is not PCF-definable (`por_undefinable_of_stable`).
At higher types the relation is the "hereditary" stability of Berry's model;
we do not compare it with the stable function space of dI-domains, which the
notes do not develop either. As the notes remark, the stable model is still
not fully abstract for PCF.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The left sequential or `or(x, y) = if(x, true, y)` is stable. -/
theorem stable_orFun : Stable fun p : Part Bool × Part Bool => orFun p.1 p.2 := sorry

/-- The stability logical relation on the domain model of PCF. Only PCF
(`p = false`) is covered: in PCF+`por` the operator `por` does not preserve
the relation at `bool`, since parallel or is not stable (`not_stable_porFun`),
so the analogue of `StableRel.op` fails for `p = true`. -/
abbrev StableRel : (τ : Ty) → (Fin 3 → (model false).ty τ) → Prop :=
  MeetLogRel (model false)

namespace StableRel

end StableRel

end Silmaril.Calculi.PCF
