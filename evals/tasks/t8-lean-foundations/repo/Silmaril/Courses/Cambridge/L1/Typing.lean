module

public import Silmaril.Courses.Cambridge.L1.Reduction

/-! # Cambridge L1 typing and type inference

the notes §2.2, collected on p. 30. A type environment is a finite partial map
from locations to the sole location type `intref`. The inference function uses
the same unbounded integer syntax as the mathematical semantics.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

inductive Ty where
  | int
  | bool
  | unit
  deriving DecidableEq, Repr

inductive LocTy where
  | intref
  deriving DecidableEq, Repr

abbrev TypeEnv := Finmap (fun _ : Loc => LocTy)

inductive HasType (Γ : TypeEnv) : Expr → Ty → Prop where
  | int (n : Int) : HasType Γ (.int n) .int
  | bool (b : Bool) : HasType Γ (.bool b) .bool
  | add {left right : Expr} (hl : HasType Γ left .int) (hr : HasType Γ right .int) :
      HasType Γ (.binary left .add right) .int
  | ge {left right : Expr} (hl : HasType Γ left .int) (hr : HasType Γ right .int) :
      HasType Γ (.binary left .ge right) .bool
  | cond {test yes no : Expr} {T : Ty}
      (hc : HasType Γ test .bool) (hy : HasType Γ yes T) (hn : HasType Γ no T) :
      HasType Γ (.cond test yes no) T
  | assign {loc : Loc} {e : Expr} (present : loc ∈ Γ) (he : HasType Γ e .int) :
      HasType Γ (.assign loc e) .unit
  | deref {loc : Loc} (present : loc ∈ Γ) : HasType Γ (.deref loc) .int
  | skip : HasType Γ .skip .unit
  | seq {left right : Expr} {T : Ty}
      (hl : HasType Γ left .unit) (hr : HasType Γ right T) :
      HasType Γ (.seq left right) T
  | loop {test body : Expr}
      (ht : HasType Γ test .bool) (hb : HasType Γ body .unit) :
      HasType Γ (.loop test body) .unit

/-- Syntax-directed type inference, returning `none` exactly for untypable terms. -/
def infer (Γ : TypeEnv) : Expr → Option Ty
  | .int _ => some .int
  | .bool _ => some .bool
  | .binary left .add right =>
      if infer Γ left = some .int ∧ infer Γ right = some .int then some .int else none
  | .binary left .ge right =>
      if infer Γ left = some .int ∧ infer Γ right = some .int then some .bool else none
  | .cond test yes no =>
      match infer Γ test, infer Γ yes, infer Γ no with
      | some .bool, some tyYes, some tyNo =>
          if tyYes = tyNo then some tyYes else none
      | _, _, _ => none
  | .assign loc e =>
      if loc ∈ Γ ∧ infer Γ e = some .int then some .unit else none
  | .deref loc => if loc ∈ Γ then some .int else none
  | .skip => some .unit
  | .seq left right =>
      if infer Γ left = some .unit then infer Γ right else none
  | .loop test body =>
      if infer Γ test = some .bool ∧ infer Γ body = some .unit then some .unit else none

theorem infer_complete {Γ : TypeEnv} {e : Expr} {T : Ty}
    (typing : HasType Γ e T) : infer Γ e = some T := sorry

theorem infer_sound {Γ : TypeEnv} : ∀ e T, infer Γ e = some T → HasType Γ e T := sorry

theorem infer_iff_hasType {Γ : TypeEnv} {e : Expr} {T : Ty} :
    infer Γ e = some T ↔ HasType Γ e T := sorry

/-- Theorem 5: type inference decides typeability and returns a witness. -/
theorem typeable_iff_infer_isSome {Γ : TypeEnv} {e : Expr} :
    (∃ T, HasType Γ e T) ↔ (infer Γ e).isSome = true := sorry

/-- Theorem 7: one expression has at most one type under a fixed environment. -/
theorem HasType.unique {Γ : TypeEnv} {e : Expr} {T U : Ty}
    (first : HasType Γ e T) (second : HasType Γ e U) : T = U := sorry

/-- Theorem 6: checking a proposed type is decidable. -/
instance (Γ : TypeEnv) (e : Expr) (T : Ty) : Decidable (HasType Γ e T) :=
  decidable_of_iff (infer Γ e = some T) infer_iff_hasType

end Silmaril.Courses.Cambridge.L1
