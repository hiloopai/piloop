module

public import Silmaril.Courses.Cambridge.L1.Exercises.TypingDerivations

/-! # Cambridge L1 Exercises 9–10: typing integer-returning assignment

With `(assign1')`, assignment reduces to an integer, so it must itself have
type `int`. With `(seq1')`, every value can be discarded by sequencing, so the
first expression may have any type. The second expression determines the
sequence's result type. These are the only changed typing rules.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises

/-! ## Exercise 9: the old assignment type is not preserved -/

def resultTypeEnv : TypeEnv := (∅ : TypeEnv).insert "l" .intref

theorem old_assignment_typed : HasType resultTypeEnv resultStart.expr .unit := sorry

theorem old_assignment_reduces :
    ValueAssignStep resultStart ⟨.int 1, orderStore.insert "l" 1⟩ := sorry

theorem old_assignment_result_not_unit :
    ¬ HasType resultTypeEnv (.int 1) .unit := sorry

/-! ## Revised typing rules -/

inductive ValueHasType (Γ : TypeEnv) : Expr → Ty → Prop where
  | int (n : Int) : ValueHasType Γ (.int n) .int
  | bool (b : Bool) : ValueHasType Γ (.bool b) .bool
  | add {left right : Expr}
      (hl : ValueHasType Γ left .int) (hr : ValueHasType Γ right .int) :
      ValueHasType Γ (.binary left .add right) .int
  | ge {left right : Expr}
      (hl : ValueHasType Γ left .int) (hr : ValueHasType Γ right .int) :
      ValueHasType Γ (.binary left .ge right) .bool
  | cond {test yes no : Expr} {T : Ty}
      (hc : ValueHasType Γ test .bool)
      (hy : ValueHasType Γ yes T) (hn : ValueHasType Γ no T) :
      ValueHasType Γ (.cond test yes no) T
  | assign {loc : Loc} {e : Expr}
      (present : loc ∈ Γ) (he : ValueHasType Γ e .int) :
      ValueHasType Γ (.assign loc e) .int
  | deref {loc : Loc} (present : loc ∈ Γ) :
      ValueHasType Γ (.deref loc) .int
  | skip : ValueHasType Γ .skip .unit
  | seq {left right : Expr} {L T : Ty}
      (hl : ValueHasType Γ left L) (hr : ValueHasType Γ right T) :
      ValueHasType Γ (.seq left right) T
  | loop {test body : Expr} {B : Ty}
      (ht : ValueHasType Γ test .bool) (hb : ValueHasType Γ body B) :
      ValueHasType Γ (.loop test body) .unit

/-- The revised inference algorithm for Exercise 10. -/
def inferValue (Γ : TypeEnv) : Expr → Option Ty
  | .int _ => some .int
  | .bool _ => some .bool
  | .binary left .add right =>
      if inferValue Γ left = some .int ∧ inferValue Γ right = some .int then
        some .int else none
  | .binary left .ge right =>
      if inferValue Γ left = some .int ∧ inferValue Γ right = some .int then
        some .bool else none
  | .cond test yes no =>
      match inferValue Γ test, inferValue Γ yes, inferValue Γ no with
      | some .bool, some tyYes, some tyNo =>
          if tyYes = tyNo then some tyYes else none
      | _, _, _ => none
  | .assign loc e =>
      if loc ∈ Γ ∧ inferValue Γ e = some .int then some .int else none
  | .deref loc => if loc ∈ Γ then some .int else none
  | .skip => some .unit
  | .seq left right =>
      if (inferValue Γ left).isSome then inferValue Γ right else none
  | .loop test body =>
      if inferValue Γ test = some .bool ∧ (inferValue Γ body).isSome then
        some .unit else none

theorem inferValue_complete {Γ : TypeEnv} {e : Expr} {T : Ty}
    (typing : ValueHasType Γ e T) : inferValue Γ e = some T := sorry

theorem inferValue_sound {Γ : TypeEnv} :
    ∀ e T, inferValue Γ e = some T → ValueHasType Γ e T := sorry

theorem inferValue_iff_hasType {Γ : TypeEnv} {e : Expr} {T : Ty} :
    inferValue Γ e = some T ↔ ValueHasType Γ e T := sorry

theorem ValueHasType.unique {Γ : TypeEnv} {e : Expr} {T U : Ty}
    (first : ValueHasType Γ e T) (second : ValueHasType Γ e U) : T = U := sorry

instance (Γ : TypeEnv) (e : Expr) (T : Ty) : Decidable (ValueHasType Γ e T) :=
  decidable_of_iff (inferValue Γ e = some T) inferValue_iff_hasType

theorem value_assignment_typed :
    ValueHasType resultTypeEnv resultStart.expr .int := sorry

theorem value_sequence_typed :
    ValueHasType assignmentTypeEnv (.seq (.int 1) (.int 2)) .int := sorry

theorem revised_inference_examples :
    inferValue resultTypeEnv resultStart.expr = some .int ∧
    inferValue assignmentTypeEnv (.seq (.int 1) (.int 2)) = some .int := sorry

/-- The revised rules restore type preservation for every variant step. -/
theorem value_assignment_preservation {Γ : TypeEnv} {before after : Config} {T : Ty}
    (typed : ValueHasType Γ before.expr T)
    (step : ValueAssignStep before after) : ValueHasType Γ after.expr T := sorry

end Silmaril.Courses.Cambridge.L1.Exercises
