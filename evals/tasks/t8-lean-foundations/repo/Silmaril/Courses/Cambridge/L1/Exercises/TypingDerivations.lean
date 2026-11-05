module

public import Silmaril.Courses.Cambridge.L1.Exercises.MachineIntegers

/-! # Cambridge L1 Exercises 7–8: typing derivations

The proofs are derivation trees built from the rules on p. 30. They do not use
the executable inferencer. Exercise 8's environment includes the unused `l3`
binding specified in the question.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises

def assignmentTypeEnv : TypeEnv :=
  ((∅ : TypeEnv).insert "l0" .intref).insert "l1" .intref

/-- Exercise 7: `(l0 := 7); (l1 := (!l0 + 2))` has type `unit`. -/
theorem assignment_type_derivation :
    HasType assignmentTypeEnv assignmentStart.expr .unit := sorry

def lectureTypeEnv : TypeEnv :=
  (((∅ : TypeEnv).insert "l1" .intref).insert "l2" .intref).insert "l3" .intref

/-- Exercise 8: the complete slide 32 expression has type `unit`. -/
theorem lecture_type_derivation :
    HasType lectureTypeEnv lectureStart.expr .unit := sorry

end Silmaril.Courses.Cambridge.L1.Exercises
