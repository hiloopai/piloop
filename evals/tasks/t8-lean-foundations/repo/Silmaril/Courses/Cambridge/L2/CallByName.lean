module

public import Silmaril.Courses.Cambridge.L2.Reduction

/-! # Cambridge L2 call-by-name reduction, §4.2

The application rules on p. 56 evaluate the function but substitute the
argument without evaluating it. Other constructs retain their collected rules;
in particular, `let` remains strict. This relation can therefore expose a
different order of store effects than `Step`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

inductive NameStep : Config → Config → Prop where
  | add (left right : Int) (s : Store) :
      NameStep ⟨.binary (.int left) .add (.int right), s⟩ ⟨.int (left + right), s⟩
  | ge (left right : Int) (s : Store) :
      NameStep ⟨.binary (.int left) .ge (.int right), s⟩ ⟨.bool (left ≥ right), s⟩
  | binaryLeft {left left' right : Expr} {op : Op} {s s' : Store}
      (inner : NameStep ⟨left, s⟩ ⟨left', s'⟩) :
      NameStep ⟨.binary left op right, s⟩ ⟨.binary left' op right, s'⟩
  | binaryRight {left right right' : Expr} {op : Op} {s s' : Store}
      (value : IsValue left) (inner : NameStep ⟨right, s⟩ ⟨right', s'⟩) :
      NameStep ⟨.binary left op right, s⟩ ⟨.binary left op right', s'⟩
  | deref {loc : Loc} {s : Store} {n : Int} (present : L1.Store.lookup s loc = some n) :
      NameStep ⟨.deref loc, s⟩ ⟨.int n, s⟩
  | assignValue {loc : Loc} {s : Store} {n : Int} (present : loc ∈ s) :
      NameStep ⟨.assign loc (.int n), s⟩ ⟨.skip, s.insert loc n⟩
  | assignStep {loc : Loc} {e e' : Expr} {s s' : Store}
      (inner : NameStep ⟨e, s⟩ ⟨e', s'⟩) :
      NameStep ⟨.assign loc e, s⟩ ⟨.assign loc e', s'⟩
  | seqSkip {e : Expr} {s : Store} :
      NameStep ⟨.seq .skip e, s⟩ ⟨e, s⟩
  | seqStep {left left' right : Expr} {s s' : Store}
      (inner : NameStep ⟨left, s⟩ ⟨left', s'⟩) :
      NameStep ⟨.seq left right, s⟩ ⟨.seq left' right, s'⟩
  | ifTrue {yes no : Expr} {s : Store} :
      NameStep ⟨.cond (.bool true) yes no, s⟩ ⟨yes, s⟩
  | ifFalse {yes no : Expr} {s : Store} :
      NameStep ⟨.cond (.bool false) yes no, s⟩ ⟨no, s⟩
  | ifStep {test test' yes no : Expr} {s s' : Store}
      (inner : NameStep ⟨test, s⟩ ⟨test', s'⟩) :
      NameStep ⟨.cond test yes no, s⟩ ⟨.cond test' yes no, s'⟩
  | while {test body : Expr} {s : Store} :
      NameStep ⟨.loop test body, s⟩
        ⟨.cond test (.seq body (.loop test body)) .skip, s⟩
  | appLeft {function function' argument : Expr} {s s' : Store}
      (inner : NameStep ⟨function, s⟩ ⟨function', s'⟩) :
      NameStep ⟨.app function argument, s⟩ ⟨.app function' argument, s'⟩
  | appFun {domain : Ty} {body argument : Expr} {s : Store} :
      NameStep ⟨.app (.lam domain body) argument, s⟩ ⟨body.beta argument, s⟩
  | letStep {type : Ty} {value value' body : Expr} {s s' : Store}
      (inner : NameStep ⟨value, s⟩ ⟨value', s'⟩) :
      NameStep ⟨.letE type value body, s⟩ ⟨.letE type value' body, s'⟩
  | letValue {type : Ty} {value body : Expr} {s : Store}
      (ready : IsValue value) :
      NameStep ⟨.letE type value body, s⟩ ⟨body.beta value, s⟩
  | letRecFun {domain codomain : Ty} {functionBody body : Expr} {s : Store} :
      NameStep ⟨.letRecFun domain codomain functionBody body, s⟩
        ⟨body.beta (Expr.recClosure domain codomain functionBody), s⟩

abbrev NameSteps : Config → Config → Prop := Relation.ReflTransGen NameStep

def nameLts : Cslib.LTS Config Unit := ⟨fun before _ after => NameStep before after⟩

theorem nameSteps_iff_reachable {before after : Config} :
    NameSteps before after ↔ nameLts.CanReach before after := sorry

end Silmaril.Courses.Cambridge.L2
