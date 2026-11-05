module

public import Silmaril.Courses.Cambridge.L2.CallByName
public import Silmaril.Courses.Cambridge.L2.Typing

/-! # Cambridge L2, Exercise 23

The outer function has type `unit → bool` and is applied to `skip`. Its body
applies a function that ignores its parameter to `l := 1`, then reads `l`.
Call by value runs the assignment first, producing `true`; call by name drops
the unused argument, producing `false` from the initial store `l ↦ 0`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.EvaluationStrategies

open Silmaril.Courses.Cambridge.L2

def Γ : TypeEnv :=
  { TypeEnv.empty with locs := (∅ : L1.TypeEnv).insert "l" .intref }

def initialStore : Store := L1.Store.fromList [("l", 0)]
def updatedStore : Store := initialStore.insert "l" 1

def readBool : Expr :=
  .cond (.binary (.deref "l") .ge (.int 1)) (.bool true) (.bool false)

def innerFunction : Expr := .lam .unit readBool
def innerCall : Expr := .app innerFunction (.assign "l" (.int 1))
def function : Expr := .lam .unit innerCall
def program : Expr := .app function .skip

theorem program_hasType : HasType Γ program .bool := sorry

def start : Config := ⟨program, initialStore⟩
def afterOuter : Config := ⟨innerCall, initialStore⟩

def valueAfterAssign : Config :=
  ⟨.app innerFunction .skip, updatedStore⟩
def valueAfterInner : Config := ⟨readBool, updatedStore⟩
def valueAfterRead : Config :=
  ⟨.cond (.binary (.int 1) .ge (.int 1)) (.bool true) (.bool false), updatedStore⟩
def valueAfterCompare : Config :=
  ⟨.cond (.bool true) (.bool true) (.bool false), updatedStore⟩
def valueResult : Config := ⟨.bool true, updatedStore⟩

theorem value_trace :
    lts.MTr start [(), (), (), (), (), ()] valueResult := sorry

theorem value_store : L1.Store.lookup valueResult.store "l" = some 1 := sorry

def nameAfterInner : Config := ⟨readBool, initialStore⟩
def nameAfterRead : Config :=
  ⟨.cond (.binary (.int 0) .ge (.int 1)) (.bool true) (.bool false), initialStore⟩
def nameAfterCompare : Config :=
  ⟨.cond (.bool false) (.bool true) (.bool false), initialStore⟩
def nameResult : Config := ⟨.bool false, initialStore⟩

theorem name_trace :
    nameLts.MTr start [(), (), (), (), ()] nameResult := sorry

theorem name_store : L1.Store.lookup nameResult.store "l" = some 0 := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.EvaluationStrategies
