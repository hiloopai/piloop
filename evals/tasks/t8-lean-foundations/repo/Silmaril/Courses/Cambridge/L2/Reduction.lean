module

public import Silmaril.Courses.Cambridge.L2.Substitution
public import Silmaril.Semantics.Operational.Execution

/-! # Cambridge L2 call-by-value reduction, §§4.2, 4.4 and 4.6

`Step` contains the collected rules on pp. 65–66. Application evaluates its
function and then its argument before beta substitution. `let` is strict too.
The recursive-function rule constructs the self-referential function printed
on p. 61; swapping its two bound indices puts the parameter and recursive name
under the binders of the new `let rec` in the right order.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

inductive IsValue : Expr → Prop where
  | int (n : Int) : IsValue (.int n)
  | bool (b : Bool) : IsValue (.bool b)
  | skip : IsValue .skip
  | lam (domain : Ty) (body : Expr) : IsValue (.lam domain body)

def Expr.isValue : Expr → Bool
  | .int _ | .bool _ | .skip | .lam _ _ => true
  | _ => false

theorem Expr.isValue_iff {e : Expr} : e.isValue = true ↔ IsValue e := sorry

/-- Exchange the parameter and recursive-function binders in a function body. -/
def swapFirstTwo : Nat → Nat
  | 0 => 1
  | 1 => 0
  | index + 2 => index + 2

/-- The function substituted for the recursive name by `(letrecfun)`. -/
def Expr.recClosure (domain codomain : Ty) (functionBody : Expr) : Expr :=
  .lam domain
    (.letRecFun domain codomain
      (functionBody.rename (DeBruijn.liftN 2 Nat.succ))
      (functionBody.rename swapFirstTwo))

/-- In the closure, a recursive call refers to the new `f` and outer `y`. -/
theorem recClosure_binding_example :
    Expr.recClosure .int .int (.app (.bvar 1) (.bvar 0)) =
      (RawExpr.lam "y" .int
        (.letRecFun "f" "x" .int .int
          (.app (.var "f") (.var "x"))
          (.app (.var "f") (.var "y")))).resolve [] := sorry

structure Config where
  expr : Expr
  store : Store
  deriving DecidableEq

/-- The L2 call-by-value small-step relation. -/
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
  | deref {loc : Loc} {s : Store} {n : Int} (present : L1.Store.lookup s loc = some n) :
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
  | appLeft {function function' argument : Expr} {s s' : Store}
      (inner : Step ⟨function, s⟩ ⟨function', s'⟩) :
      Step ⟨.app function argument, s⟩ ⟨.app function' argument, s'⟩
  | appRight {function argument argument' : Expr} {s s' : Store}
      (value : IsValue function) (inner : Step ⟨argument, s⟩ ⟨argument', s'⟩) :
      Step ⟨.app function argument, s⟩ ⟨.app function argument', s'⟩
  | appFun {domain : Ty} {body argument : Expr} {s : Store}
      (value : IsValue argument) :
      Step ⟨.app (.lam domain body) argument, s⟩ ⟨body.beta argument, s⟩
  | letStep {type : Ty} {value value' body : Expr} {s s' : Store}
      (inner : Step ⟨value, s⟩ ⟨value', s'⟩) :
      Step ⟨.letE type value body, s⟩ ⟨.letE type value' body, s'⟩
  | letValue {type : Ty} {value body : Expr} {s : Store}
      (ready : IsValue value) :
      Step ⟨.letE type value body, s⟩ ⟨body.beta value, s⟩
  | letRecFun {domain codomain : Ty} {functionBody body : Expr} {s : Store} :
      Step ⟨.letRecFun domain codomain functionBody body, s⟩
        ⟨body.beta (Expr.recClosure domain codomain functionBody), s⟩

/-- The course's unlabelled transition relation in the shared execution layer. -/
def lts : Cslib.LTS Config Unit := ⟨fun before _ after => Step before after⟩

abbrev Steps : Config → Config → Prop := Relation.ReflTransGen Step

theorem steps_iff_reachable {before after : Config} :
    Steps before after ↔ lts.CanReach before after := sorry

def Terminal (config : Config) : Prop := ∀ next, ¬ Step config next

def Stuck (config : Config) : Prop :=
  ¬ IsValue config.expr ∧ Terminal config

end Silmaril.Courses.Cambridge.L2
