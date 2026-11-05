module

public import Silmaril.Courses.Cambridge.L1.Exercises.ValueAssignmentTyping

/-! # Cambridge L1 Exercise 11: operator and sequence order

The printed L1 semantics and appendix interpreter both reduce the left
operator argument first, and finish the left side of a sequence before its
right side. The existing Exercise 4 traces show how reversing operator order
changes an observable store value. The sequence trace below checks the other
half of the question in the original reduction relation.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Exercises

theorem original_operator_order_trace :
    lts.MTr orderStart [(), (), (), (), ()] leftResult := sorry

def sequenceStart : Config :=
  ⟨.seq (.assign "l" (.int 1)) (.assign "l" (.int 2)), orderStore⟩

def sequenceAfter1 : Config :=
  ⟨.seq .skip (.assign "l" (.int 2)), orderStore.insert "l" 1⟩

def sequenceAfter2 : Config :=
  ⟨.assign "l" (.int 2), sequenceAfter1.store⟩

def sequenceResult : Config :=
  ⟨.skip, sequenceAfter2.store.insert "l" 2⟩

theorem sequence_left_first_trace :
    lts.MTr sequenceStart [(), (), ()] sequenceResult := sorry

theorem sequence_result_store :
    Store.lookup sequenceResult.store "l" = some 2 := sorry

end Silmaril.Courses.Cambridge.L1.Exercises
