module

public import Silmaril.Courses.Cambridge.L1.Exercises.Variants

/-! # Cambridge L1 Exercise 6: machine integers

The notes' mathematical L1 uses arbitrary integers. The appendix OCaml
interpreter instead uses `int`, whose width is `Sys.int_size` (typically 31 or
63 bits). Its addition wraps modulo `2^width`; its comparison is signed.

Here the width is a parameter and integer literals and store contents are
bitvectors of that width. This gives a finite-width variant with modular
addition and signed comparison, including widths chosen to match an OCaml
platform. The finite map represents stores extensionally, as in `L1.Store`;
no correspondence with the OCaml implementation is proved here.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises.Machine

inductive Expr (w : Nat) where
  | int (value : BitVec w)
  | bool (value : Bool)
  | binary (left : Expr w) (op : Op) (right : Expr w)
  | cond (test yes no : Expr w)
  | assign (loc : Loc) (value : Expr w)
  | deref (loc : Loc)
  | skip
  | seq (left right : Expr w)
  | loop (test body : Expr w)
  deriving DecidableEq

inductive IsValue {w : Nat} : Expr w → Prop where
  | int (n : BitVec w) : IsValue (.int n)
  | bool (b : Bool) : IsValue (.bool b)
  | skip : IsValue .skip

def Expr.isValue {w : Nat} : Expr w → Bool
  | .int _ | .bool _ | .skip => true
  | _ => false

abbrev Store (w : Nat) := Finmap (fun _ : Loc => BitVec w)

namespace Store

def lookup {w : Nat} (s : Store w) (loc : Loc) : Option (BitVec w) :=
  Finmap.lookup loc s

def update {w : Nat} (s : Store w) (loc : Loc) (value : BitVec w) :
    Option (Store w) :=
  if loc ∈ s then some (s.insert loc value) else none

def fromList {w : Nat} (entries : List (Loc × BitVec w)) : Store w :=
  entries.foldl (fun s (loc, value) => s.insert loc value) ∅

end Store

structure Config (w : Nat) where
  expr : Expr w
  store : Store w
  deriving DecidableEq

/-- The thirteen L1 rules with OCaml's finite signed integer operations. -/
inductive Step {w : Nat} : Config w → Config w → Prop where
  | add (left right : BitVec w) (s : Store w) :
      Step ⟨.binary (.int left) .add (.int right), s⟩ ⟨.int (left + right), s⟩
  | ge (left right : BitVec w) (s : Store w) :
      Step ⟨.binary (.int left) .ge (.int right), s⟩
        ⟨.bool (left.toInt ≥ right.toInt), s⟩
  | binaryLeft {left left' right : Expr w} {op : Op} {s s' : Store w}
      (inner : Step ⟨left, s⟩ ⟨left', s'⟩) :
      Step ⟨.binary left op right, s⟩ ⟨.binary left' op right, s'⟩
  | binaryRight {left right right' : Expr w} {op : Op} {s s' : Store w}
      (value : IsValue left) (inner : Step ⟨right, s⟩ ⟨right', s'⟩) :
      Step ⟨.binary left op right, s⟩ ⟨.binary left op right', s'⟩
  | deref {loc : Loc} {s : Store w} {n : BitVec w}
      (present : Store.lookup s loc = some n) :
      Step ⟨.deref loc, s⟩ ⟨.int n, s⟩
  | assignValue {loc : Loc} {s : Store w} {n : BitVec w} (present : loc ∈ s) :
      Step ⟨.assign loc (.int n), s⟩ ⟨.skip, s.insert loc n⟩
  | assignStep {loc : Loc} {e e' : Expr w} {s s' : Store w}
      (inner : Step ⟨e, s⟩ ⟨e', s'⟩) :
      Step ⟨.assign loc e, s⟩ ⟨.assign loc e', s'⟩
  | seqSkip {e : Expr w} {s : Store w} :
      Step ⟨.seq .skip e, s⟩ ⟨e, s⟩
  | seqStep {left left' right : Expr w} {s s' : Store w}
      (inner : Step ⟨left, s⟩ ⟨left', s'⟩) :
      Step ⟨.seq left right, s⟩ ⟨.seq left' right, s'⟩
  | ifTrue {yes no : Expr w} {s : Store w} :
      Step ⟨.cond (.bool true) yes no, s⟩ ⟨yes, s⟩
  | ifFalse {yes no : Expr w} {s : Store w} :
      Step ⟨.cond (.bool false) yes no, s⟩ ⟨no, s⟩
  | ifStep {test test' yes no : Expr w} {s s' : Store w}
      (inner : Step ⟨test, s⟩ ⟨test', s'⟩) :
      Step ⟨.cond test yes no, s⟩ ⟨.cond test' yes no, s'⟩
  | while {test body : Expr w} {s : Store w} :
      Step ⟨.loop test body, s⟩
        ⟨.cond test (.seq body (.loop test body)) .skip, s⟩

def lts (w : Nat) : Cslib.LTS (Config w) Unit :=
  ⟨fun before _ after => Step before after⟩

/-- The appendix's pattern-matching one-step interpreter at a chosen width. -/
def reduce {w : Nat} (s : Store w) : Expr w → Option (Config w)
  | .int _ | .bool _ | .skip => none
  | .binary left op right =>
      match left, op, right with
      | .int a, .add, .int b => some ⟨.int (a + b), s⟩
      | .int a, .ge, .int b => some ⟨.bool (a.toInt ≥ b.toInt), s⟩
      | _, _, _ =>
          if left.isValue then
            (reduce s right).map (fun next => ⟨.binary left op next.expr, next.store⟩)
          else
            (reduce s left).map (fun next => ⟨.binary next.expr op right, next.store⟩)
  | .cond test yes no =>
      match test with
      | .bool true => some ⟨yes, s⟩
      | .bool false => some ⟨no, s⟩
      | _ => (reduce s test).map (fun next => ⟨.cond next.expr yes no, next.store⟩)
  | .assign loc e =>
      match e with
      | .int n => (Store.update s loc n).map (fun nextStore => ⟨.skip, nextStore⟩)
      | _ => (reduce s e).map (fun next => ⟨.assign loc next.expr, next.store⟩)
  | .deref loc => (Store.lookup s loc).map (fun n => ⟨.int n, s⟩)
  | .seq left right =>
      match left with
      | .skip => some ⟨right, s⟩
      | _ => (reduce s left).map (fun next => ⟨.seq next.expr right, next.store⟩)
  | .loop test body => some ⟨.cond test (.seq body (.loop test body)) .skip, s⟩

def reduceConfig {w : Nat} (c : Config w) : Option (Config w) :=
  reduce c.store c.expr

/-! A four-bit instance makes the representability difference explicit. -/

def sampleStore : Store 4 := Store.fromList [("l", BitVec.ofInt 4 0)]

def overflowStart : Config 4 :=
  ⟨.binary (.int (BitVec.ofInt 4 7)) .add (.int (BitVec.ofInt 4 1)), sampleStore⟩

def overflowResult : Config 4 := ⟨.int (BitVec.ofInt 4 (-8)), sampleStore⟩

theorem overflow_step : Step overflowStart overflowResult := sorry

theorem overflow_interpreter : reduceConfig overflowStart = some overflowResult := sorry

theorem overflow_signed_value : (BitVec.ofInt 4 (7 + 1)).toInt = -8 := sorry

theorem signed_comparison :
    reduceConfig (⟨.binary (.int (BitVec.ofInt 4 (-1))) .ge
      (.int (BitVec.ofInt 4 1)), sampleStore⟩ : Config 4) =
      some ⟨.bool false, sampleStore⟩ := sorry

end Silmaril.Courses.Cambridge.L1.Exercises.Machine
