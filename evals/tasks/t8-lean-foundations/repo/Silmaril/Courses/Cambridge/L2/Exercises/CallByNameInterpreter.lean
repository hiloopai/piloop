module

public import Silmaril.Courses.Cambridge.L2.Interpreter
public import Silmaril.Courses.Cambridge.L2.Exercises.EvaluationStrategies

/-! # Cambridge L2, Exercise 26

The executable reducer is checked against the proved Exercise 23 traces.
Further examples distinguish evaluating an argument once, evaluating it twice,
and discarding a divergent argument. A terminating recursive function checks
the shared `let rec` implementation in both modes.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.CallByNameInterpreter

open Silmaril.Courses.Cambridge.L2

theorem value_matches_exercise23 :
    runFuel .value 10 EvaluationStrategies.start =
      some EvaluationStrategies.valueResult := sorry

theorem name_matches_exercise23 :
    runFuel .name 10 EvaluationStrategies.start =
      some EvaluationStrategies.nameResult := sorry

def bump : Expr :=
  .assign "l" (.binary (.deref "l") .add (.int 1))

def duplicate : Expr :=
  .app (.lam .unit (.seq (.bvar 0) (.bvar 0))) bump

def duplicateStart : Config :=
  ⟨duplicate, EvaluationStrategies.initialStore⟩

theorem value_runs_argument_once :
    runFuel .value 20 duplicateStart =
      some ⟨.skip, EvaluationStrategies.initialStore.insert "l" 1⟩ := sorry

theorem name_runs_argument_twice :
    runFuel .name 20 duplicateStart =
      some ⟨.skip, (EvaluationStrategies.initialStore.insert "l" 1).insert "l" 2⟩ := sorry

def diverges : Expr := .loop (.bool true) .skip
def ignoresArgument : Config :=
  ⟨.app (.lam .unit .skip) diverges, L1.Store.empty⟩

theorem name_discards_divergence :
    runFuel .name 2 ignoresArgument =
      some ⟨.skip, L1.Store.empty⟩ := sorry

/-- This equation checks exhaustion of a particular fuel budget, not an infinite trace. -/
theorem value_forces_divergence :
    runFuel .value 20 ignoresArgument = none := sorry

/-- The body computes a recursive sum of ones down from its argument. -/
def recursiveSum : Expr :=
  .letRecFun .int .int
    (.cond (.binary (.bvar 0) .ge (.int 1))
      (.binary (.int 1) .add
        (.app (.bvar 1) (.binary (.bvar 0) .add (.int (-1)))))
      (.int 0))
    (.app (.bvar 0) (.int 2))

theorem recursiveSum_value :
    runFuel .value 100 ⟨recursiveSum, L1.Store.empty⟩ =
      some ⟨.int 2, L1.Store.empty⟩ := sorry

theorem recursiveSum_name :
    runFuel .name 100 ⟨recursiveSum, L1.Store.empty⟩ =
      some ⟨.int 2, L1.Store.empty⟩ := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.CallByNameInterpreter
