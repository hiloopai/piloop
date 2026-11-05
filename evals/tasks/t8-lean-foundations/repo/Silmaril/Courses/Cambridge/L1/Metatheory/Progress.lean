module

public import Silmaril.Courses.Cambridge.L1.Metatheory.Inversion

/-! # Cambridge L1 progress and Exercise 15

The store covers the typing environment when every typed location is already
allocated. Canonical-form lemmas identify typed values so that the primitive
operator, conditional, assignment, and sequence rules can fire.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

def StoreCovers (Γ : TypeEnv) (s : Store) : Prop :=
  ∀ loc, loc ∈ Γ → loc ∈ s

/-- A well-typed configuration with a covering store cannot be stuck. -/
theorem not_stuck {Γ : TypeEnv} {e : Expr} {T : Ty} {s : Store}
    (typing : HasType Γ e T) (covers : StoreCovers Γ s) :
    ¬ Stuck ⟨e, s⟩ := sorry

end Silmaril.Courses.Cambridge.L1
