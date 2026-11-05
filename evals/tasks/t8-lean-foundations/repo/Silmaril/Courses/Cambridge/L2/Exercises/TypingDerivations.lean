module

public import Silmaril.Courses.Cambridge.L2.Typing

/-! # Cambridge L2, Exercise 21

The conditional with integer test has no derivation. In the second expression,
the inner Boolean parameter shadows the outer integer parameter only within
the inner function; resolving the names makes the two `x` occurrences point to
different binders.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.TypingDerivations

open Silmaril.Courses.Cambridge.L2

def badConditional : Expr :=
  .cond (.int 6) (.int 7) (.int 8)

theorem badConditional_untypable :
    ¬ ∃ T, HasType TypeEnv.empty badConditional T := sorry

def shadowing : RawExpr :=
  .lam "x" .int
    (.binary (.var "x") .add
      (.app
        (.lam "x" .bool (.cond (.var "x") (.int 3) (.int 4)))
        (.bool true)))

theorem shadowing_infer :
    infer TypeEnv.empty (shadowing.resolve []) = some (.arr .int .int) := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.TypingDerivations
