module

public import Silmaril.Courses.Cambridge.L2.CallByName

/-! # Cambridge L2 executable reduction, §§4.5 and Exercise 26

`reduce` implements one small step for the complete L2 grammar. The mode
changes only application: CBV evaluates the argument before beta substitution;
CBN substitutes it immediately. `runFuel` distinguishes a terminal result
from an execution that has not stopped within the supplied step budget.
The terminal result can be a value or a stuck configuration. The exercise
modules prove concrete results; a general correctness theorem relating this
reducer to `Step` or `NameStep` is not provided here.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

inductive EvaluationMode where
  | value
  | name
  deriving DecidableEq, Repr

def reduce (mode : EvaluationMode) (store : Store) : Expr → Option (Expr × Store)
  | .binary (.int left) .add (.int right) =>
      some (.int (left + right), store)
  | .binary (.int left) .ge (.int right) =>
      some (.bool (left ≥ right), store)
  | .binary left op right =>
      if left.isValue then
        (reduce mode store right).map fun (right', store') =>
          (.binary left op right', store')
      else
        (reduce mode store left).map fun (left', store') =>
          (.binary left' op right, store')
  | .cond (.bool true) yes _ => some (yes, store)
  | .cond (.bool false) _ no => some (no, store)
  | .cond test yes no =>
      (reduce mode store test).map fun (test', store') =>
        (.cond test' yes no, store')
  | .assign loc (.int n) =>
      (L1.Store.update store loc n).map fun store' => (.skip, store')
  | .assign loc value =>
      (reduce mode store value).map fun (value', store') =>
        (.assign loc value', store')
  | .deref loc =>
      (L1.Store.lookup store loc).map fun n => (.int n, store)
  | .seq .skip right => some (right, store)
  | .seq left right =>
      (reduce mode store left).map fun (left', store') =>
        (.seq left' right, store')
  | .loop test body =>
      some (.cond test (.seq body (.loop test body)) .skip, store)
  | .app (.lam domain body) argument =>
      match mode with
      | .name => some (body.beta argument, store)
      | .value =>
          if argument.isValue then some (body.beta argument, store)
          else
            (reduce mode store argument).map fun (argument', store') =>
              (.app (.lam domain body) argument', store')
  | .app function argument =>
      if function.isValue then
        match mode with
        | .name => none
        | .value =>
            (reduce mode store argument).map fun (argument', store') =>
              (.app function argument', store')
      else
        (reduce mode store function).map fun (function', store') =>
          (.app function' argument, store')
  | .letE type value body =>
      if value.isValue then some (body.beta value, store)
      else
        (reduce mode store value).map fun (value', store') =>
          (.letE type value' body, store')
  | .letRecFun domain codomain functionBody body =>
      some (body.beta (Expr.recClosure domain codomain functionBody), store)
  | .int _ | .bool _ | .skip | .bvar _ | .fvar _ | .lam _ _ => none

def Config.reduce (mode : EvaluationMode) (config : Config) : Option Config :=
  (Silmaril.Courses.Cambridge.L2.reduce mode config.store config.expr).map
    fun (expr, store) => ⟨expr, store⟩

def runFuel (mode : EvaluationMode) : Nat → Config → Option Config
  | fuel, config =>
      match config.reduce mode with
      | none => some config
      | some next =>
          match fuel with
          | 0 => none
          | fuel + 1 => runFuel mode fuel next

end Silmaril.Courses.Cambridge.L2
