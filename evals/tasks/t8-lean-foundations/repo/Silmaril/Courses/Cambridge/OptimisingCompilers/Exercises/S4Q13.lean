module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.PointsTo

/-! # Exercise sheet 4, question 13 (Andersen, and flow-sensitive points-to)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 13.


Answer. Informally `p` points to `c` and then to `d`, and `q` to `e`. Andersen's
relation is `pt(p) = {c, d}`, `pt(q) = {e}` (`PointsTo.notesExample_andersen`;
the integer store `*p = 3` generates no pointer constraint), with domain
`{p, q}` and range `{c, d, e}`.

Per program point it is more precise: after `p = &c` (where `*p = 3` runs) `p`
points only to `c`, so the store cannot touch `d`. The equations are *forward*:
`out(n) = in(n) \ kill(n) ∪ gen(n)` with `in(n) = ⋃ p ∈ pred(n), out(p)`, where
an assignment to `x` kills all pairs `(x, _)` and generates its targets
(`flowStep`). `flow_example`: at the store, `p` points only to `c`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q13

open PointsTo

variable {V : Type*} [DecidableEq V]

/-- The flow-sensitive transfer function of a statement on a points-to
relation, with strong updates of variables and weak updates through pointers. -/
def flowStep (pt : AVal V → Set (AVal V)) : Stmt V → AVal V → Set (AVal V)
  | .new x ℓ => Function.update pt (.var x) {.new ℓ}
  | .null x => Function.update pt (.var x) {.null}
  | .addr x y => Function.update pt (.var x) {.var y}
  | .copy x y => Function.update pt (.var x) (pt (.var y))
  | .load x y => Function.update pt (.var x) (⋃ z ∈ pt (.var y), pt z)
  | .store x y => fun a => pt a ∪ {b | a ∈ pt (.var x) ∧ b ∈ pt (.var y)}

/-- Points-to relations before each statement of straight-line code. -/
def flowBefore (pt₀ : AVal V → Set (AVal V)) : List (Stmt V) → List (AVal V → Set (AVal V))
  | [] => [pt₀]
  | s :: ss => pt₀ :: flowBefore (flowStep pt₀ s) ss

/-- The pointer statements of the program. The integer store `*p = 3`, which
changes no pointer, runs between the first two. -/
def program : List (Stmt String) := [.addr "p" "c", .addr "p" "d", .addr "q" "e"]

/-- At the store (before `p = &d`), `p` points only to `c`. -/
theorem flow_example :
    ((flowBefore (fun _ => ∅) program)[1]?).map (fun pt => pt (.var "p")) = some {.var "c"} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q13
