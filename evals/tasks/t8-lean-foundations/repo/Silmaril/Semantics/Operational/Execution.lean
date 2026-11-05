module

public import Cslib.Foundations.Semantics.LTS.Execution
public import Cslib.Foundations.Semantics.LTS.Relation
public import Silmaril.Foundations.InfiniteTrace

/-!
# Executable exploration of operational semantics

An execution layer using CSLib's labelled transition systems and finite
executions, together with Silmaril's infinite traces. A finite successor enumerator has an explicit correctness contract;
neither finiteness nor decidability is assumed for arbitrary SOS rule systems.
-/

@[expose] public section

namespace Silmaril.Semantics.Operational

universe u v

variable {State : Type u} {Label : Type v}

/-- A complete, executable list of outgoing transitions. Duplicate successors
are harmless. An instance is an obligation of a concrete interpreter. -/
structure Enumerator (lts : Cslib.LTS State Label) where
  next : State → List (Label × State)
  correct : ∀ s a t, (a, t) ∈ next s ↔ lts.Tr s a t

/-- All executions of exactly `fuel` transitions, recording labels and endpoints.
Fuel exhaustion is a bound on exploration, not a claim about termination. -/
def explore (next : State → List (Label × State)) : Nat → State → List (List Label × State)
  | 0, s => [([], s)]
  | n + 1, s => (next s).flatMap fun (a, t) =>
      (explore next n t).map fun (labels, result) => (a :: labels, result)

/-- Successful exploration also supplies all the intermediate states through
CSLib's execution representation. -/
theorem Enumerator.execution_of_mem {lts : Cslib.LTS State Label} (enumerator : Enumerator lts)
    {n : Nat} {s t : State} {labels : List Label}
    (h : (labels, t) ∈ explore enumerator.next n s) :
    ∃ states, lts.Execution s labels t states := sorry

/-- An empty successor list really means stuckness only for a complete enumerator. -/
theorem Enumerator.next_eq_nil_iff {lts : Cslib.LTS State Label} (enumerator : Enumerator lts)
    (s : State) : enumerator.next s = [] ↔ ∀ a t, ¬ lts.Tr s a t := sorry

/-- Divergence uses actual transitions, including their labels. -/
def Diverges (lts : Cslib.LTS State Label) (s : State) : Prop :=
  InfiniteTrace lts.UnlabelledTr s

/-- A finite execution followed by divergence is divergent. -/
theorem Diverges.prefix {lts : Cslib.LTS State Label} {s t : State} {labels : List Label}
    (path : lts.MTr s labels t) (tail : Diverges lts t) : Diverges lts s := sorry

/-- A stuck state cannot be mistaken for a divergent one. -/
theorem not_diverges_of_stuck {lts : Cslib.LTS State Label} {s : State}
    (stuck : ∀ a t, ¬ lts.Tr s a t) : ¬ Diverges lts s := sorry

/-- Divergence is failure of CSLib strong normalization for the unlabelled transitions. -/
theorem Diverges.iff_not_sn {lts : Cslib.LTS State Label} {s : State} :
    Diverges lts s ↔ ¬ Relation.SN lts.UnlabelledTr s := sorry

/-- No state of a terminating CSLib LTS diverges. -/
theorem not_diverges_of_terminating (lts : Cslib.LTS State Label) [lts.Terminating]
    (s : State) : ¬ Diverges lts s := sorry

end Silmaril.Semantics.Operational
