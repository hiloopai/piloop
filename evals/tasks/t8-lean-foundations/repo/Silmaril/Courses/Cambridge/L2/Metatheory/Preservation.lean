module

public import Silmaril.Courses.Cambridge.L2.Metatheory.Substitution

/-! # Cambridge L2 type preservation, Exercise 25

Theorem 19 on pp. 58–59 extends L1 preservation with the application, `let`,
and recursive-function cases. The recursive case uses a typed self-reference
closure; its de Bruijn renamings preserve both the parameter and recursive
function assumptions.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

def StoreCovers (Γ : TypeEnv) (s : Store) : Prop :=
  ∀ loc, loc ∈ Γ.locs → loc ∈ s

/-- Typing and store coverage remain valid after one L2 call-by-value step. -/
theorem preservation {Γ : TypeEnv} {before after : Config} {T : Ty}
    (typed : HasType Γ before.expr T)
    (covers : StoreCovers Γ before.store)
    (step : Step before after) :
    HasType Γ after.expr T ∧ StoreCovers Γ after.store := sorry

end Silmaril.Courses.Cambridge.L2
