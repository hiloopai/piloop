module

public import Mathlib.Data.Int.Basic

/-! # Cambridge L1 abstract syntax

The grammar in the course notes (2025–26), §2.1,
pp. 13 and 29. Locations are opaque names: the object language has no operation
that computes with them. Integers are unbounded, as in the printed semantics.
The source is the lecture notes.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

abbrev Loc := String

inductive Op where
  | add
  | ge
  deriving DecidableEq, Repr

inductive Expr where
  | int : Int → Expr
  | bool : Bool → Expr
  | binary : Expr → Op → Expr → Expr
  | cond : Expr → Expr → Expr → Expr
  | assign : Loc → Expr → Expr
  | deref : Loc → Expr
  | skip : Expr
  | seq : Expr → Expr → Expr
  | loop : Expr → Expr → Expr
  deriving DecidableEq, Repr

/-- The three value forms `n`, `b`, and `skip` on p. 14. -/
inductive IsValue : Expr → Prop where
  | int (n : Int) : IsValue (.int n)
  | bool (b : Bool) : IsValue (.bool b)
  | skip : IsValue .skip

def Expr.isValue : Expr → Bool
  | .int _ | .bool _ | .skip => true
  | _ => false

theorem Expr.isValue_iff {e : Expr} : e.isValue = true ↔ IsValue e := sorry

end Silmaril.Courses.Cambridge.L1
