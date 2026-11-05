module

public import Silmaril.Courses.Cambridge.L1
public import Mathlib.Data.Finset.Basic

/-! # Cambridge L2 syntax and alpha conversion, §§4.1 and 4.6

`RawExpr` presents the named grammar on p. 65. `Expr` retains free variable
names but represents bound occurrences by de Bruijn indices, as described on
pp. 52–53. Resolution chooses the nearest binder, so two raw terms that differ
only by a sound binder renaming resolve to the same expression.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

abbrev Loc := L1.Loc
abbrev Store := L1.Store
abbrev Op := L1.Op
abbrev Var := String

inductive Ty where
  | int
  | bool
  | unit
  | arr (domain codomain : Ty)
  deriving DecidableEq, Repr

inductive RawExpr where
  | int (value : Int)
  | bool (value : Bool)
  | binary (left : RawExpr) (op : Op) (right : RawExpr)
  | cond (test yes no : RawExpr)
  | assign (loc : Loc) (value : RawExpr)
  | deref (loc : Loc)
  | skip
  | seq (left right : RawExpr)
  | loop (test body : RawExpr)
  | var (name : Var)
  | lam (name : Var) (domain : Ty) (body : RawExpr)
  | app (function argument : RawExpr)
  | letE (name : Var) (type : Ty) (value body : RawExpr)
  | letRecFun (function parameter : Var) (domain codomain : Ty)
      (functionBody body : RawExpr)
  deriving DecidableEq, Repr

inductive Expr where
  | int (value : Int)
  | bool (value : Bool)
  | binary (left : Expr) (op : Op) (right : Expr)
  | cond (test yes no : Expr)
  | assign (loc : Loc) (value : Expr)
  | deref (loc : Loc)
  | skip
  | seq (left right : Expr)
  | loop (test body : Expr)
  | bvar (index : Nat)
  | fvar (name : Var)
  | lam (domain : Ty) (body : Expr)
  | app (function argument : Expr)
  | letE (type : Ty) (value body : Expr)
  | letRecFun (domain codomain : Ty) (functionBody body : Expr)
  deriving DecidableEq, Repr

/-- The current binder is at index zero; enclosing binders follow it. -/
def RawExpr.resolve (bound : List Var) : RawExpr → Expr
  | .int n => .int n
  | .bool b => .bool b
  | .binary left op right =>
      .binary (left.resolve bound) op (right.resolve bound)
  | .cond test yes no =>
      .cond (test.resolve bound) (yes.resolve bound) (no.resolve bound)
  | .assign loc e => .assign loc (e.resolve bound)
  | .deref loc => .deref loc
  | .skip => .skip
  | .seq left right => .seq (left.resolve bound) (right.resolve bound)
  | .loop test body => .loop (test.resolve bound) (body.resolve bound)
  | .var name =>
      match bound.idxOf? name with
      | some index => .bvar index
      | none => .fvar name
  | .lam name domain body => .lam domain (body.resolve (name :: bound))
  | .app function argument =>
      .app (function.resolve bound) (argument.resolve bound)
  | .letE name type value body =>
      .letE type (value.resolve bound) (body.resolve (name :: bound))
  | .letRecFun function parameter domain codomain functionBody body =>
      .letRecFun domain codomain
        (functionBody.resolve (parameter :: function :: bound))
        (body.resolve (function :: bound))

/-- Alpha equivalence of raw syntax is equality after scope resolution. -/
def AlphaEq (left right : RawExpr) : Prop :=
  left.resolve [] = right.resolve []

instance (left right : RawExpr) : Decidable (AlphaEq left right) :=
  inferInstanceAs (Decidable (left.resolve [] = right.resolve []))

theorem alpha_refl (e : RawExpr) : AlphaEq e e := sorry
theorem alpha_symm {left right : RawExpr} (h : AlphaEq left right) :
    AlphaEq right left := sorry
theorem alpha_trans {first middle last : RawExpr}
    (h₁ : AlphaEq first middle) (h₂ : AlphaEq middle last) :
    AlphaEq first last := sorry

/-- The printed free-variable equations, including both binders of `let rec`. -/
def RawExpr.freeVars : RawExpr → Finset Var
  | .int _ | .bool _ | .deref _ | .skip => ∅
  | .binary left _ right | .seq left right | .loop left right |
    .app left right => left.freeVars ∪ right.freeVars
  | .cond test yes no => test.freeVars ∪ yes.freeVars ∪ no.freeVars
  | .assign _ e => e.freeVars
  | .var name => {name}
  | .lam name _ body => body.freeVars.erase name
  | .letE name _ value body => value.freeVars ∪ (body.freeVars.erase name)
  | .letRecFun function parameter _ _ functionBody body =>
      ((functionBody.freeVars.erase parameter).erase function) ∪
        (body.freeVars.erase function)

/-- Free names remain visible in the locally nameless expression. -/
def Expr.freeVars : Expr → Finset Var
  | .int _ | .bool _ | .deref _ | .skip | .bvar _ => ∅
  | .binary left _ right | .seq left right | .loop left right |
    .app left right => left.freeVars ∪ right.freeVars
  | .cond test yes no => test.freeVars ∪ yes.freeVars ∪ no.freeVars
  | .assign _ e | .lam _ e => e.freeVars
  | .fvar name => {name}
  | .letE _ value body | .letRecFun _ _ value body =>
      value.freeVars ∪ body.freeVars

/-- Every bound index is accounted for by an ambient or enclosing binder. -/
def Expr.Scoped (depth : Nat) : Expr → Prop
  | .bvar index => index < depth
  | .int _ | .bool _ | .deref _ | .skip | .fvar _ => True
  | .binary left _ right | .seq left right | .loop left right |
    .app left right => Scoped depth left ∧ Scoped depth right
  | .cond test yes no =>
      Scoped depth test ∧ Scoped depth yes ∧ Scoped depth no
  | .assign _ e => Scoped depth e
  | .lam _ body => Scoped (depth + 1) body
  | .letE _ value body => Scoped depth value ∧ Scoped (depth + 1) body
  | .letRecFun _ _ functionBody body =>
      Scoped (depth + 2) functionBody ∧ Scoped (depth + 1) body

def Expr.Closed (e : Expr) : Prop := e.Scoped 0 ∧ e.freeVars = ∅

/-- Scope resolution never creates a dangling de Bruijn index. -/
theorem RawExpr.resolve_scoped (e : RawExpr) (bound : List Var) :
    (e.resolve bound).Scoped bound.length := sorry

/-- Example from p. 52: changing a binder name preserves the binding graph. -/
theorem alpha_rename_example :
    AlphaEq
      (.lam "x" .int (.binary (.var "x") .add (.var "z")))
      (.lam "y" .int (.binary (.var "y") .add (.var "z"))) := sorry

/-- Capturing `z` changes which binder its occurrence points to. -/
theorem alpha_capture_counterexample :
    ¬ AlphaEq
      (.lam "x" .int (.binary (.var "x") .add (.var "z")))
      (.lam "z" .int (.binary (.var "z") .add (.var "z"))) := sorry

end Silmaril.Courses.Cambridge.L2
