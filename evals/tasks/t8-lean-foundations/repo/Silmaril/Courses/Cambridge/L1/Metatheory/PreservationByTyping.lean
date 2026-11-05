module

public import Silmaril.Courses.Cambridge.L1.Metatheory.Preservation

/-! # Cambridge L1 Exercise 17: preservation by typing induction

This is a second proof of the expression-type clause of Theorem 3. It follows
the typing derivation, then inverts the reduction in each typing-rule case.
It does not use `Step.preserves_type`; only the store-domain lemma is reused to
obtain the coverage clause of the full theorem.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

/-- The full preservation statement, using the alternate type proof. -/
theorem preservation_by_typing {Γ : TypeEnv} {before after : Config} {T : Ty}
    (typed : HasType Γ before.expr T)
    (covers : StoreCovers Γ before.store)
    (step : Step before after) :
    HasType Γ after.expr T ∧ StoreCovers Γ after.store := sorry

end Silmaril.Courses.Cambridge.L1
