module

public import Silmaril.Courses.Cambridge.L1.Typing

/-! # Cambridge L1 exercises 1–3 (p. 31)

The expressions below are abstract syntax trees, so every parenthesization and
sequencing choice is explicit. The step theorems are complete derivations in
the inductive relation, and the traces compose them in the shared LTS.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises

def plus (left right : Expr) : Expr := .binary left .add right
def ge (left right : Expr) : Expr := .binary left .ge right
def read (loc : Loc) : Expr := .deref loc
def dec (loc : Loc) : Expr := .assign loc (plus (read loc) (.int (-1)))

/-! ## Exercise 1: factorial using only addition, comparison, and loops -/

/-- Repeated addition multiplies the old accumulator in `l3` by the input in `l4`. -/
def multiplyBody : Expr :=
  .seq (.assign "l2" (plus (read "l2") (read "l3"))) (dec "l4")

def multiplyLoop : Expr := .loop (ge (read "l4") (.int 1)) multiplyBody

def factorialBody : Expr :=
  .seq (.assign "l3" (read "l2"))
    (.seq (.assign "l2" (.int 0))
      (.seq (.assign "l4" (read "l1"))
        (.seq multiplyLoop (dec "l1"))))

/-- Input is the initial integer in `l1`; the final value is returned by `!l2`.
All four locations must initially belong to the finite store. -/
def factorial : Expr :=
  .seq (.assign "l2" (.int 1))
    (.seq (.loop (ge (read "l1") (.int 1)) factorialBody) (read "l2"))

/-- The input convention for Exercise 1: `l1` holds a nonnegative integer.
The other three locations start allocated so assignment can update them. -/
def factorialInitial (n : Int) : Store :=
  Store.fromList [("l1", n), ("l2", 0), ("l3", 0), ("l4", 0)]

theorem factorial_initial_lookup (n : Int) :
    Store.lookup (factorialInitial n) "l1" = some n := sorry

def factorialTypeEnv : TypeEnv :=
  (((∅ : TypeEnv).insert "l1" .intref).insert "l2" .intref).insert "l3" .intref
    |>.insert "l4" .intref

theorem factorial_has_type : HasType factorialTypeEnv factorial .int := sorry

/-! ## Exercise 2: full derivations for the two assignments -/

def assignmentStore : Store := Store.fromList [("l0", 0), ("l1", 0)]
def afterFirstStore : Store := assignmentStore.insert "l0" 7
def afterSecondStore : Store := afterFirstStore.insert "l1" 9

def assignmentRight : Expr := .assign "l1" (plus (read "l0") (.int 2))

def assignmentStart : Config :=
  ⟨.seq (.assign "l0" (.int 7)) assignmentRight, assignmentStore⟩
def assignmentAfter1 : Config := ⟨.seq .skip assignmentRight, afterFirstStore⟩
def assignmentAfter2 : Config := ⟨assignmentRight, afterFirstStore⟩
def assignmentAfter3 : Config :=
  ⟨.assign "l1" (plus (.int 7) (.int 2)), afterFirstStore⟩
def assignmentAfter4 : Config := ⟨.assign "l1" (.int 9), afterFirstStore⟩
def assignmentResult : Config := ⟨.skip, afterSecondStore⟩

theorem assignment_trace :
    lts.MTr assignmentStart [(), (), (), (), ()] assignmentResult := sorry

theorem assignment_result_lookup : Store.lookup assignmentResult.store "l1" = some 9 := sorry

theorem assignment_result_terminal : Terminal assignmentResult := sorry

/-! ## Exercise 3: first four steps of the lecture's initial L1 program -/

def lectureStore : Store := Store.fromList [("l1", 3), ("l2", 0)]
def lectureAfterInitStore : Store := lectureStore.insert "l2" 0
def lectureGuard : Expr := ge (read "l1") (.int 1)
def lectureBody : Expr :=
  .seq (.assign "l2" (plus (read "l2") (read "l1"))) (dec "l1")
def lectureLoop : Expr := .loop lectureGuard lectureBody

def lectureStart : Config :=
  ⟨.seq (.assign "l2" (.int 0)) lectureLoop, lectureStore⟩
def lectureAfter1 : Config := ⟨.seq .skip lectureLoop, lectureAfterInitStore⟩
def lectureAfter2 : Config := ⟨lectureLoop, lectureAfterInitStore⟩
def lectureAfter3 : Config :=
  ⟨.cond lectureGuard (.seq lectureBody lectureLoop) .skip, lectureAfterInitStore⟩
def lectureAfter4 : Config :=
  ⟨.cond (ge (.int 3) (.int 1)) (.seq lectureBody lectureLoop) .skip,
    lectureAfterInitStore⟩

theorem lecture_first_four_steps :
    lts.MTr lectureStart [(), (), (), ()] lectureAfter4 := sorry

end Silmaril.Courses.Cambridge.L1.Exercises
