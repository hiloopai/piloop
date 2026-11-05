module

public import Silmaril.Courses.Cambridge.L1.Store
public import Silmaril.Semantics.Operational.Execution

/-! # Cambridge L1 small-step semantics

The thirteen rules of the notes §2.1, collected on p. 29. A configuration pairs an
expression with a finite store. `Step` follows the printed left-to-right operator
order and keeps assignment restricted to locations already in the store.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

structure Config where
  expr : Expr
  store : Store
  deriving DecidableEq

inductive Step : Config → Config → Prop where
  | add (left right : Int) (s : Store) :
      Step ⟨.binary (.int left) .add (.int right), s⟩ ⟨.int (left + right), s⟩
  | ge (left right : Int) (s : Store) :
      Step ⟨.binary (.int left) .ge (.int right), s⟩ ⟨.bool (left ≥ right), s⟩
  | binaryLeft {left left' right : Expr} {op : Op} {s s' : Store}
      (inner : Step ⟨left, s⟩ ⟨left', s'⟩) :
      Step ⟨.binary left op right, s⟩ ⟨.binary left' op right, s'⟩
  | binaryRight {left right right' : Expr} {op : Op} {s s' : Store}
      (value : IsValue left) (inner : Step ⟨right, s⟩ ⟨right', s'⟩) :
      Step ⟨.binary left op right, s⟩ ⟨.binary left op right', s'⟩
  | deref {loc : Loc} {s : Store} {n : Int} (present : Store.lookup s loc = some n) :
      Step ⟨.deref loc, s⟩ ⟨.int n, s⟩
  | assignValue {loc : Loc} {s : Store} {n : Int} (present : loc ∈ s) :
      Step ⟨.assign loc (.int n), s⟩ ⟨.skip, s.insert loc n⟩
  | assignStep {loc : Loc} {e e' : Expr} {s s' : Store}
      (inner : Step ⟨e, s⟩ ⟨e', s'⟩) :
      Step ⟨.assign loc e, s⟩ ⟨.assign loc e', s'⟩
  | seqSkip {e : Expr} {s : Store} :
      Step ⟨.seq .skip e, s⟩ ⟨e, s⟩
  | seqStep {left left' right : Expr} {s s' : Store}
      (inner : Step ⟨left, s⟩ ⟨left', s'⟩) :
      Step ⟨.seq left right, s⟩ ⟨.seq left' right, s'⟩
  | ifTrue {yes no : Expr} {s : Store} :
      Step ⟨.cond (.bool true) yes no, s⟩ ⟨yes, s⟩
  | ifFalse {yes no : Expr} {s : Store} :
      Step ⟨.cond (.bool false) yes no, s⟩ ⟨no, s⟩
  | ifStep {test test' yes no : Expr} {s s' : Store}
      (inner : Step ⟨test, s⟩ ⟨test', s'⟩) :
      Step ⟨.cond test yes no, s⟩ ⟨.cond test' yes no, s'⟩
  | while {test body : Expr} {s : Store} :
      Step ⟨.loop test body, s⟩
        ⟨.cond test (.seq body (.loop test body)) .skip, s⟩

/-- The unlabelled transition system of §1 and §2.1, embedded in CSLib. -/
def lts : Cslib.LTS Config Unit := ⟨fun before _ after => Step before after⟩

/-- Zero or more of the course's single-step reductions. -/
abbrev Steps : Config → Config → Prop := Relation.ReflTransGen Step

def Terminal (config : Config) : Prop := ∀ next, ¬ Step config next

def Stuck (config : Config) : Prop :=
  ¬ IsValue config.expr ∧ Terminal config

end Silmaril.Courses.Cambridge.L1
