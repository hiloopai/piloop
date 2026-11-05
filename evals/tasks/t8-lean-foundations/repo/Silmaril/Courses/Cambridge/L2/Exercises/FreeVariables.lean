module

public import Silmaril.Courses.Cambridge.L2.Syntax

/-! # Cambridge L2, Exercise 19

The five printed expressions are represented in named syntax. For each, the
free-variable answer is checked against `RawExpr.freeVars`, and the resolved
expression gives its abstract syntax tree up to alpha equivalence. In the third
tree, the innermost `y` shadows the other two and resolves to index zero.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.FreeVariables

open Silmaril.Courses.Cambridge.L2

def first : RawExpr :=
  .binary (.var "x") .add (.app (.lam "y" .int (.var "z")) (.int 2))

def second : RawExpr :=
  .binary (.var "x") .add (.lam "y" .int (.var "z"))

def third : RawExpr :=
  .lam "y" .int (.lam "y" .int (.lam "y" .int (.var "y")))

def fourth : RawExpr := .deref "l0"

def fifth : RawExpr :=
  .loop (.binary (.deref "l0") .ge (.var "y")) (.assign "l0" (.var "x"))

theorem first_freeVars : first.freeVars = {"x", "z"} := sorry
theorem first_tree : first.resolve [] =
    .binary (.fvar "x") .add (.app (.lam .int (.fvar "z")) (.int 2)) := sorry

theorem second_freeVars : second.freeVars = {"x", "z"} := sorry
theorem second_tree : second.resolve [] =
    .binary (.fvar "x") .add (.lam .int (.fvar "z")) := sorry

theorem third_freeVars : third.freeVars = ∅ := sorry
theorem third_tree : third.resolve [] =
    .lam .int (.lam .int (.lam .int (.bvar 0))) := sorry

theorem fourth_freeVars : fourth.freeVars = ∅ := sorry
theorem fourth_tree : fourth.resolve [] = .deref "l0" := sorry

theorem fifth_freeVars : fifth.freeVars = {"x", "y"} := sorry
theorem fifth_tree : fifth.resolve [] =
    .loop (.binary (.deref "l0") .ge (.fvar "y"))
      (.assign "l0" (.fvar "x")) := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.FreeVariables
