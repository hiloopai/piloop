module

public import Silmaril.Courses.Cambridge.L2.Interpreter

/-! # Cambridge L2 closure interpreter, Exercise 27

An ordinary closure stores the lambda body and its lexical environment. A
recursive closure stores the recursive function body and the environment from
its definition. Applying it extends that saved environment with the argument
and the recursive closure itself, so no cyclic runtime value or syntactic
substitution is needed. This is a CBV evaluator for closed L2 programs.
The exercises prove agreement with substitution-based evaluation on three
programs; a general simulation theorem is not provided here.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

inductive RuntimeValue where
  | int (value : Int)
  | bool (value : Bool)
  | unit
  | closure (domain : Ty) (body : Expr) (environment : List RuntimeValue)
  | recursive (domain codomain : Ty) (body : Expr)
      (environment : List RuntimeValue)
  deriving Repr

abbrev RuntimeEnv := List RuntimeValue

/-- Fuel bounds evaluation depth, including recursive calls and loops. -/
def evalClosureFuel : Nat → RuntimeEnv → Store → Expr →
    Option (RuntimeValue × Store)
  | 0, _, _, _ => none
  | fuel + 1, environment, store, expression =>
      match expression with
      | .int n => some (.int n, store)
      | .bool b => some (.bool b, store)
      | .skip => some (.unit, store)
      | .bvar index => (environment[index]?).map fun value => (value, store)
      | .fvar _ => none
      | .lam domain body => some (.closure domain body environment, store)
      | .binary left op right => do
          let (leftValue, afterLeft) ← evalClosureFuel fuel environment store left
          let (rightValue, afterRight) ← evalClosureFuel fuel environment afterLeft right
          match leftValue, op, rightValue with
          | .int a, .add, .int b => some (.int (a + b), afterRight)
          | .int a, .ge, .int b => some (.bool (a ≥ b), afterRight)
          | _, _, _ => none
      | .cond test yes no => do
          let (value, afterTest) ← evalClosureFuel fuel environment store test
          match value with
          | .bool true => evalClosureFuel fuel environment afterTest yes
          | .bool false => evalClosureFuel fuel environment afterTest no
          | _ => none
      | .assign loc expression => do
          let (value, afterValue) ← evalClosureFuel fuel environment store expression
          match value with
          | .int n =>
              let updated ← L1.Store.update afterValue loc n
              some (.unit, updated)
          | _ => none
      | .deref loc =>
          (L1.Store.lookup store loc).map fun n => (.int n, store)
      | .seq left right => do
          let (value, afterLeft) ← evalClosureFuel fuel environment store left
          match value with
          | .unit => evalClosureFuel fuel environment afterLeft right
          | _ => none
      | .loop test body => do
          let (value, afterTest) ← evalClosureFuel fuel environment store test
          match value with
          | .bool false => some (.unit, afterTest)
          | .bool true =>
              let (bodyValue, afterBody) ←
                evalClosureFuel fuel environment afterTest body
              match bodyValue with
              | .unit => evalClosureFuel fuel environment afterBody expression
              | _ => none
          | _ => none
      | .app function argument => do
          let (functionValue, afterFunction) ←
            evalClosureFuel fuel environment store function
          let (argumentValue, afterArgument) ←
            evalClosureFuel fuel environment afterFunction argument
          match functionValue with
          | .closure _ body saved =>
              evalClosureFuel fuel (argumentValue :: saved) afterArgument body
          | .recursive domain codomain body saved =>
              let self := RuntimeValue.recursive domain codomain body saved
              evalClosureFuel fuel (argumentValue :: self :: saved) afterArgument body
          | _ => none
      | .letE _ value body => do
          let (result, afterValue) ← evalClosureFuel fuel environment store value
          evalClosureFuel fuel (result :: environment) afterValue body
      | .letRecFun domain codomain functionBody body =>
          let self := RuntimeValue.recursive domain codomain functionBody environment
          evalClosureFuel fuel (self :: environment) store body

/-- Observe ground results in the same configuration shape as the small-step interpreter. -/
def RuntimeValue.groundExpr : RuntimeValue → Option Expr
  | .int n => some (.int n)
  | .bool b => some (.bool b)
  | .unit => some .skip
  | .closure _ _ _ | .recursive _ _ _ _ => none

def evalClosureGround (fuel : Nat) (config : Config) : Option Config := do
  let (value, store) ← evalClosureFuel fuel [] config.store config.expr
  let expression ← value.groundExpr
  some ⟨expression, store⟩

end Silmaril.Courses.Cambridge.L2
