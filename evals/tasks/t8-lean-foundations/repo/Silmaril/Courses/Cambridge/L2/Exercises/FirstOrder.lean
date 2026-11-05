module

public import Silmaril.Courses.Cambridge.L2.Typing

/-! # Cambridge L2, Exercise 22

This is one possible first-order, fully applied function language. A function
type lists all its base-typed parameters and has a base result. Lambda and call
syntax are n-ary; the call rule requires one argument of each parameter type.
Consequently a function cannot be passed or returned as an argument/result,
and no well-typed call can supply only a prefix of its arguments.

Unlike the curried L2 type grammar on p. 50, the list representation treats
`int → bool → int` as a two-argument function with one atomic call.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2.Exercises.FirstOrder

inductive BaseTy where
  | int
  | bool
  | unit
  deriving DecidableEq, Repr

inductive Ty where
  | base (type : BaseTy)
  | fun (parameters : List BaseTy) (result : BaseTy)
  deriving DecidableEq, Repr

inductive Expr where
  | int (value : Int)
  | bool (value : Bool)
  | skip
  | var (index : Nat)
  | lam (parameters : List BaseTy) (body : Expr)
  | call (function : Expr) (arguments : List Expr)
  deriving Repr

/-- Parameter zero is nearest to the body, matching de Bruijn indexing. -/
def pushParameters (Γ : List Ty) (parameters : List BaseTy) : List Ty :=
  parameters.map Ty.base ++ Γ

mutual

inductive HasType : List Ty → Expr → Ty → Prop where
  | int {Γ : List Ty} (n : Int) : HasType Γ (.int n) (.base .int)
  | bool {Γ : List Ty} (b : Bool) : HasType Γ (.bool b) (.base .bool)
  | skip {Γ : List Ty} : HasType Γ .skip (.base .unit)
  | var {Γ : List Ty} {index : Nat} {T : Ty} (lookup : Γ[index]? = some T) :
      HasType Γ (.var index) T
  | lam {Γ : List Ty} {parameters : List BaseTy} {result : BaseTy} {body : Expr}
      (typing : HasType (pushParameters Γ parameters) body (.base result)) :
      HasType Γ (.lam parameters body) (.fun parameters result)
  | call {Γ : List Ty} {function : Expr} {arguments : List Expr}
      {parameters : List BaseTy} {result : BaseTy}
      (functionType : HasType Γ function (.fun parameters result))
      (argumentsType : ArgsHaveType Γ arguments parameters) :
      HasType Γ (.call function arguments) (.base result)

inductive ArgsHaveType : List Ty → List Expr → List BaseTy → Prop where
  | nil {Γ : List Ty} : ArgsHaveType Γ [] []
  | cons {Γ : List Ty} {argument : Expr} {arguments : List Expr}
      {parameter : BaseTy} {parameters : List BaseTy}
      (head : HasType Γ argument (.base parameter))
      (tail : ArgsHaveType Γ arguments parameters) :
      ArgsHaveType Γ (argument :: arguments) (parameter :: parameters)

end

/-- Every typed call returns a base type and supplies the full arity. -/
theorem HasType.call_saturated {Γ : List Ty} {function : Expr}
    {arguments : List Expr} {T : Ty}
    (typing : HasType Γ (.call function arguments) T) :
    ∃ parameters result, T = .base result ∧
      HasType Γ function (.fun parameters result) ∧
      arguments.length = parameters.length := sorry

/-- An explicit two-parameter function cannot be called with one argument. -/
theorem partial_call_untypable :
    ¬ ∃ T, HasType []
      (.call (.lam [.int, .int] (.var 0)) [.int 1]) T := sorry

end Silmaril.Courses.Cambridge.L2.Exercises.FirstOrder
