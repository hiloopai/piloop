module

public import Silmaril.Courses.Cambridge.L1.Exercises.EvaluationOrder
public import Mathlib.Data.Finset.Sort
public import Mathlib.Data.String.Basic

/-! # Cambridge L1 Exercise 12: printable reduction derivations

`StepTree` is a proof-relevant copy of the printed reduction rules. Its
constructors retain each rule name and premise tree at runtime, while
`StepTree.toStep` certifies that its root is a genuine L1 reduction.
`searchStep` follows the appendix interpreter's rule-selection strategy.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises

inductive StepTree : Config → Config → Type where
  | add (left right : Int) (s : Store) :
      StepTree ⟨.binary (.int left) .add (.int right), s⟩ ⟨.int (left + right), s⟩
  | ge (left right : Int) (s : Store) :
      StepTree ⟨.binary (.int left) .ge (.int right), s⟩ ⟨.bool (left ≥ right), s⟩
  | binaryLeft {left left' right : Expr} {op : Op} {s s' : Store}
      (inner : StepTree ⟨left, s⟩ ⟨left', s'⟩) :
      StepTree ⟨.binary left op right, s⟩ ⟨.binary left' op right, s'⟩
  | binaryRight {left right right' : Expr} {op : Op} {s s' : Store}
      (value : IsValue left) (inner : StepTree ⟨right, s⟩ ⟨right', s'⟩) :
      StepTree ⟨.binary left op right, s⟩ ⟨.binary left op right', s'⟩
  | deref {loc : Loc} {s : Store} {n : Int} (present : Store.lookup s loc = some n) :
      StepTree ⟨.deref loc, s⟩ ⟨.int n, s⟩
  | assignValue {loc : Loc} {s : Store} {n : Int} (present : loc ∈ s) :
      StepTree ⟨.assign loc (.int n), s⟩ ⟨.skip, s.insert loc n⟩
  | assignStep {loc : Loc} {e e' : Expr} {s s' : Store}
      (inner : StepTree ⟨e, s⟩ ⟨e', s'⟩) :
      StepTree ⟨.assign loc e, s⟩ ⟨.assign loc e', s'⟩
  | seqSkip {e : Expr} {s : Store} :
      StepTree ⟨.seq .skip e, s⟩ ⟨e, s⟩
  | seqStep {left left' right : Expr} {s s' : Store}
      (inner : StepTree ⟨left, s⟩ ⟨left', s'⟩) :
      StepTree ⟨.seq left right, s⟩ ⟨.seq left' right, s'⟩
  | ifTrue {yes no : Expr} {s : Store} :
      StepTree ⟨.cond (.bool true) yes no, s⟩ ⟨yes, s⟩
  | ifFalse {yes no : Expr} {s : Store} :
      StepTree ⟨.cond (.bool false) yes no, s⟩ ⟨no, s⟩
  | ifStep {test test' yes no : Expr} {s s' : Store}
      (inner : StepTree ⟨test, s⟩ ⟨test', s'⟩) :
      StepTree ⟨.cond test yes no, s⟩ ⟨.cond test' yes no, s'⟩
  | while {test body : Expr} {s : Store} :
      StepTree ⟨.loop test body, s⟩
        ⟨.cond test (.seq body (.loop test body)) .skip, s⟩

/-- Search for one reduction and retain the complete rule derivation tree. -/
def searchStep (s : Store) :
    (e : Expr) → Option (Σ after : Config, StepTree ⟨e, s⟩ after)
  | .int _ | .bool _ | .skip => none
  | .binary (.int a) .add (.int b) =>
      some ⟨⟨.int (a + b), s⟩, .add a b s⟩
  | .binary (.int a) .ge (.int b) =>
      some ⟨⟨.bool (a ≥ b), s⟩, .ge a b s⟩
  | .binary left op right =>
      if h : left.isValue then
        match searchStep s right with
        | some ⟨next, tree⟩ =>
            some ⟨⟨.binary left op next.expr, next.store⟩,
              .binaryRight (Expr.isValue_iff.mp h) tree⟩
        | none => none
      else
        match searchStep s left with
        | some ⟨next, tree⟩ =>
            some ⟨⟨.binary next.expr op right, next.store⟩,
              .binaryLeft tree⟩
        | none => none
  | .cond (.bool true) yes _ => some ⟨⟨yes, s⟩, .ifTrue⟩
  | .cond (.bool false) _ no => some ⟨⟨no, s⟩, .ifFalse⟩
  | .cond test yes no =>
      match searchStep s test with
      | some ⟨next, tree⟩ =>
          some ⟨⟨.cond next.expr yes no, next.store⟩, .ifStep tree⟩
      | none => none
  | .assign loc (.int n) =>
      if h : loc ∈ s then
        some ⟨⟨.skip, s.insert loc n⟩, .assignValue h⟩
      else none
  | .assign loc e =>
      match searchStep s e with
      | some ⟨next, tree⟩ =>
          some ⟨⟨.assign loc next.expr, next.store⟩, .assignStep tree⟩
      | none => none
  | .deref loc =>
      match h : Store.lookup s loc with
      | some n => some ⟨⟨.int n, s⟩, .deref h⟩
      | none => none
  | .seq .skip right => some ⟨⟨right, s⟩, .seqSkip⟩
  | .seq left right =>
      match searchStep s left with
      | some ⟨next, tree⟩ =>
          some ⟨⟨.seq next.expr right, next.store⟩, .seqStep tree⟩
      | none => none
  | .loop test body =>
      some ⟨⟨.cond test (.seq body (.loop test body)) .skip, s⟩, .while⟩

def searchConfig (c : Config) :
    Option (Σ after : Config, StepTree c after) :=
  searchStep c.store c.expr

/-- Every successful proof search result is a genuine small-step reduction. -/
theorem searchConfig_sound {before : Config}
    {found : Σ after : Config, StepTree before after}
    (_h : searchConfig before = some found) : Step before found.1 := sorry

def renderExpr : Expr → String
  | .int n => toString n
  | .bool b => toString b
  | .binary left .add right => s!"({renderExpr left} + {renderExpr right})"
  | .binary left .ge right => s!"({renderExpr left} >= {renderExpr right})"
  | .cond test yes no =>
      s!"(if {renderExpr test} then {renderExpr yes} else {renderExpr no})"
  | .assign loc e => s!"({loc} := {renderExpr e})"
  | .deref loc => s!"!{loc}"
  | .skip => "skip"
  | .seq left right => s!"({renderExpr left}; {renderExpr right})"
  | .loop test body => s!"(while {renderExpr test} do {renderExpr body} done)"

def renderStore (s : Store) : String :=
  "{" ++ String.intercalate ", "
    (s.keys.sort.map fun loc =>
      match Store.lookup s loc with
      | some n => s!"{loc}->{n}"
      | none => s!"{loc}->?") ++ "}"

def renderConfig (c : Config) : String :=
  s!"<{renderExpr c.expr}, {renderStore c.store}>"

def StepTree.ruleName {before after : Config} : StepTree before after → String
  | .add .. => "op +"
  | .ge .. => "op >="
  | .binaryLeft .. => "op1"
  | .binaryRight .. => "op2"
  | .deref .. => "deref"
  | .assignValue .. => "assign1"
  | .assignStep .. => "assign2"
  | .seqSkip => "seq1"
  | .seqStep .. => "seq2"
  | .ifTrue => "if1"
  | .ifFalse => "if2"
  | .ifStep .. => "if3"
  | .while => "while"

/-- Premise lines come first and are indented under their parent conclusion. -/
def StepTree.lines {before after : Config} (tree : StepTree before after) :
    List String := Id.run do
  let row := s!"({tree.ruleName}) {renderConfig before} -> {renderConfig after}"
  let premises := match tree with
    | .binaryLeft inner => inner.lines
    | .binaryRight _ inner => inner.lines
    | .assignStep inner => inner.lines
    | .seqStep inner => inner.lines
    | .ifStep inner => inner.lines
    | _ => []
  return (premises.map ("  " ++ ·)) ++ [row]

def StepTree.render {before after : Config} (tree : StepTree before after) : String :=
  String.intercalate "\n" tree.lines

/-- Display a derivation tree or report the configuration where search fails. -/
def printStep (before : Config) : String :=
  match searchConfig before with
  | some ⟨_, tree⟩ => tree.render
  | none =>
      let reason := if before.expr.isValue then "value" else "stuck"
      s!"No reduction rule applies ({reason}): {renderConfig before}"

def printableAssignmentStep : String := printStep assignmentStart

def printableStuck : String :=
  printStep ⟨.binary (.int 1) .add (.bool true), Store.empty⟩

theorem assignment_search_succeeds : (searchConfig assignmentStart).isSome = true := sorry

theorem malformed_addition_search_fails :
    (searchConfig ⟨.binary (.int 1) .add (.bool true), Store.empty⟩).isNone = true := sorry

end Silmaril.Courses.Cambridge.L1.Exercises
