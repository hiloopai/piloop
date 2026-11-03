module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q13

/-! # Exercise sheet 4, question 14 (Andersen on a loop)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 14.


Answer. Andersen's method ignores control flow: the pointer statements are
`q = &r`, `p = q`, `p = *p`, `s = q`, `r = &s`, and the least solution is
`pt(q) = {r}`, `pt(s) = {r}`, `pt(r) = {s}`, `pt(p) = {r, s}`
(`andersen_loop`).

Per program point, the relation is the least solution of the forward
inequations of question 13 (`S4Q13.flowStep`) over the loop's flowgraph
(`Node`, `edges`; the test on line 2 and the prints are no-ops). Writing
`(p, q, r, s)` for the targets of the four variables, the relation on entry
to each node is (`perPoint_isLeast`):

| node | entry relation |
|---|---|
| 1 `q = &r` | all empty |
| 2 loop test, 3 `p = q`, 6 `r = &s`, 8 `print 42` | `({r, s}, {r}, {s}, {r})` |
| 4 `p = *p`, 5 `s = q` | `({r}, {r}, {s}, {r})` |
| 4' `print p` | `({s}, {r}, {s}, {r})` |

So inside the `then` branch `p` points to exactly one location at each point,
unlike the flow-insensitive `pt(p) = {r, s}`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q14

open PointsTo

/-- The pointer statements of the program. -/
def program : List (Stmt String) :=
  [.addr "q" "r", .copy "p" "q", .load "p" "p", .copy "s" "q", .addr "r" "s"]

/-- The least solution for `program`. -/
def ptLoop (a : AVal String) : Set (AVal String) :=
  if a = .var "p" then {.var "r", .var "s"}
  else if a = .var "q" then {.var "r"}
  else if a = .var "s" then {.var "r"}
  else if a = .var "r" then {.var "s"}
  else ∅

/-- The overall points-to relation. -/
theorem andersen_loop :
    andersen program (.var "q") = {.var "r"} ∧ andersen program (.var "s") = {.var "r"} ∧
      andersen program (.var "r") = {.var "s"} ∧
      andersen program (.var "p") = {.var "r", .var "s"} := sorry

/-! ## The points-to relation at each program point -/

/-- Nodes of the loop's flowgraph, numbered by source line; `n4'` is
`print p`. -/
inductive Node
  | n1 | n2 | n3 | n4 | n4' | n5 | n6 | n8
  deriving DecidableEq

/-- The pointer statement at each node (`none` for the test and the prints). -/
def stmt : Node → Option (Stmt String)
  | .n1 => some (.addr "q" "r")
  | .n2 => none
  | .n3 => some (.copy "p" "q")
  | .n4 => some (.load "p" "p")
  | .n4' => none
  | .n5 => some (.copy "s" "q")
  | .n6 => some (.addr "r" "s")
  | .n8 => none

/-- The edges of the flowgraph. -/
def edges : List (Node × Node) :=
  [(.n1, .n2), (.n2, .n3), (.n2, .n8), (.n3, .n4), (.n4, .n4'), (.n4', .n6), (.n3, .n5),
    (.n5, .n6), (.n6, .n2)]

/-- Points-to relations. -/
abbrev PT := AVal String → Set (AVal String)

/-- The transfer function of a node (`S4Q13.flowStep`, identity for no-ops). -/
def transfer (pt : PT) : Option (Stmt String) → PT
  | none => pt
  | some s => S4Q13.flowStep pt s

/-- Solutions of the forward inequations: for each edge `m → n`, the relation
on exit from `m` is contained in the relation on entry to `n`. -/
def Sol (X : Node → PT) : Prop := ∀ e ∈ edges, transfer (X e.1) (stmt e.1) ≤ X e.2

/-- The relation with targets `p`, `q`, `r`, `s` for the four variables. -/
def mk (p q r s : Set (AVal String)) : PT := fun a =>
  if a = .var "p" then p else if a = .var "q" then q else if a = .var "r" then r
  else if a = .var "s" then s else ∅

/-- The relation on entry to each node. -/
def expected : Node → PT
  | .n1 => mk ∅ ∅ ∅ ∅
  | .n2 | .n3 | .n6 | .n8 => mk {.var "r", .var "s"} {.var "r"} {.var "s"} {.var "r"}
  | .n4 | .n5 => mk {.var "r"} {.var "r"} {.var "s"} {.var "r"}
  | .n4' => mk {.var "s"} {.var "r"} {.var "s"} {.var "r"}

section mk

variable {p q r s P : Set (AVal String)}

end mk

/-- The points-to relation at each program point: `expected` is the least
solution of the forward inequations. -/
theorem perPoint_isLeast : IsLeast {X | Sol X} expected := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q14
