module

public import Mathlib.Data.List.Basic
public import Mathlib.Algebra.Ring.Int.Defs

/-! # Exercise sheet 1, question 3 (dead code and unknown procedures)

Source: `cam-optcomp-2025-exercise-sheet-1` in `references.bib`, question 3.


This exercise models a procedure separately by its observable behaviour on
an argument: the outputs it performs
and its result, or `none` if it does not terminate.

* (a) The assignment `z = p(x)` is dead: `z` is never used.
* (b) Deleting the call is safe when `p` has no side effects and terminates
  (`dce_safe_of_pure`), but not in general (`dce_unsafe`): a printing or
  diverging `p` is observable.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q3

/-- A procedure's behaviour on an argument: its outputs and its result
(`none` for non-termination). -/
abbrev Proc := ℤ → List ℤ × Option ℤ

/-- The behaviour of `g`: run `p x`, then `q x`, and return `q`'s result. -/
def g (p q : Proc) (x : ℤ) : List ℤ × Option ℤ :=
  match p x with
  | (o₁, some _) => ((o₁ ++ (q x).1), (q x).2)
  | (o₁, none) => (o₁, none)

/-- `g` after deleting the dead `z = p(x)`. -/
def gDce (q : Proc) (x : ℤ) : List ℤ × Option ℤ := q x

/-- (b) Deleting the call is safe for pure, terminating `p`. -/
theorem dce_safe_of_pure (p q : Proc) (hp : ∀ x, (p x).1 = [] ∧ (p x).2.isSome) :
    g p q = gDce q := sorry

/-- (b) In general it is unsafe: a `p` that prints changes the behaviour. -/
theorem dce_unsafe : ∃ p q : Proc, g p q ≠ gDce q := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q3
