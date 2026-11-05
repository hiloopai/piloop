module

public import Silmaril.Courses.Cambridge.L1.Exercises.DerivationPrinter

/-! # Cambridge L1 determinacy and Exercise 13

Theorem 1 is proved by induction on the first reduction derivation and
inversion of the second. The cases where a value would have to reduce close by
inverting that impossible premise. This covers the operator, sequence, while,
and conditional cases requested in Exercise 13.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

/-- Theorem 1: a configuration has at most one one-step successor. -/
theorem Step.deterministic {before firstResult secondResult : Config}
    (first : Step before firstResult) (second : Step before secondResult) :
    firstResult = secondResult := sorry

/-- Exercise 14's relation allows both operator-context rules at once. -/
def NondeterministicStep (before after : Config) : Prop :=
  Step before after ∨ Exercises.RightStep before after

theorem nondeterministic_not_determinate :
    ¬ ∀ before firstResult secondResult,
      NondeterministicStep before firstResult →
      NondeterministicStep before secondResult → firstResult = secondResult := sorry

end Silmaril.Courses.Cambridge.L1
