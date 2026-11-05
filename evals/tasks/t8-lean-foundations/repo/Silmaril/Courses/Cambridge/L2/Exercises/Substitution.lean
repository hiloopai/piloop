module

public import Silmaril.Courses.Cambridge.L2.Substitution

/-! # Cambridge L2, Exercise 20

The three substitutions are checked after resolving the printed named terms
to alpha-invariant syntax. The first answer uses `w` as a fresh binder; any
other name that leaves the binding graph unchanged gives the same result.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.Substitution

open Silmaril.Courses.Cambridge.L2

/-- Exercise 20.1: `{fun x:int → y / z} (fun y:int → z y)`. -/
theorem first :
    ((RawExpr.lam "y" .int (.app (.var "z") (.var "y"))).resolve []).substFree
        "z" ((RawExpr.lam "x" .int (.var "y")).resolve []) =
      (RawExpr.lam "w" .int
        (.app (.lam "x" .int (.var "y")) (.var "w"))).resolve [] := sorry

/-- Exercise 20.2: `{fun x:int → x / x} (fun y:int → x y)`. -/
theorem second :
    ((RawExpr.lam "y" .int (.app (.var "x") (.var "y"))).resolve []).substFree
        "x" ((RawExpr.lam "x" .int (.var "x")).resolve []) =
      (RawExpr.lam "y" .int
        (.app (.lam "x" .int (.var "x")) (.var "y"))).resolve [] := sorry

/-- Exercise 20.3: `{fun x:int → x / x} (fun x:int → x x)`. -/
theorem third :
    ((RawExpr.lam "x" .int (.app (.var "x") (.var "x"))).resolve []).substFree
        "x" ((RawExpr.lam "x" .int (.var "x")).resolve []) =
      (RawExpr.lam "x" .int (.app (.var "x") (.var "x"))).resolve [] := sorry

/-- Shifting a replacement through a binder preserves its ambient index. -/
theorem ambient_index_not_captured :
    (Expr.lam .int (.fvar "z")).substFree "z" (.bvar 0) =
      .lam .int (.bvar 1) := sorry

/-- The notes' `y + 2` example also avoids capture by the lambda's `y`. -/
theorem lecture_capture_example :
    ((RawExpr.lam "y" .int
      (.binary (.var "x") .add (.var "y"))).resolve []).substFree
        "x" ((RawExpr.binary (.var "y") .add (.int 2)).resolve []) =
      (RawExpr.lam "z" .int
        (.binary (.binary (.var "y") .add (.int 2)) .add (.var "z"))).resolve [] := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.Substitution
