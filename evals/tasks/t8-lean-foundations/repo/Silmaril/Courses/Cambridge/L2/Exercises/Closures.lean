module

public import Silmaril.Courses.Cambridge.L2.ClosureInterpreter
public import Silmaril.Courses.Cambridge.L2.Exercises.CallByNameInterpreter

/-! # Cambridge L2, Exercise 27

The closure evaluator and substitution-based CBV interpreter agree on three
programs. The lexical-scope example returns `3`; looking up `x` in the caller's
environment would instead use `100` and return `102`. The other examples
check recursive calls and a store effect.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.Closures

open Silmaril.Courses.Cambridge.L2

def lexicalScope : Expr :=
  .letE .int (.int 1)
    (.letE (.arr .int .int)
      (.lam .int (.binary (.bvar 1) .add (.bvar 0)))
      (.letE .int (.int 100) (.app (.bvar 1) (.int 2))))

def lexicalStart : Config := ⟨lexicalScope, L1.Store.empty⟩
def lexicalResult : Config := ⟨.int 3, L1.Store.empty⟩

theorem lexical_substitution :
    runFuel .value 40 lexicalStart = some lexicalResult := sorry

theorem lexical_closure :
    evalClosureGround 40 lexicalStart = some lexicalResult := sorry

def recursiveStart : Config :=
  ⟨CallByNameInterpreter.recursiveSum, L1.Store.empty⟩

theorem recursion_substitution :
    runFuel .value 100 recursiveStart =
      some ⟨.int 2, L1.Store.empty⟩ := sorry

theorem recursion_closure :
    evalClosureGround 100 recursiveStart =
      some ⟨.int 2, L1.Store.empty⟩ := sorry

theorem store_substitution :
    runFuel .value 30 EvaluationStrategies.start =
      some EvaluationStrategies.valueResult := sorry

theorem store_closure :
    evalClosureGround 30 EvaluationStrategies.start =
      some EvaluationStrategies.valueResult := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.Closures
