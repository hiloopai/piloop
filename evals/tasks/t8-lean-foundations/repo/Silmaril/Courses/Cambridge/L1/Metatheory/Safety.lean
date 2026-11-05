module

public import Silmaril.Courses.Cambridge.L1.Metatheory.TypingUniqueness

/-! # Cambridge L1 safety (Theorem 4)

Theorem 3 composes along finite small-step traces. Progress then applies to
every reachable configuration, giving Theorem 4 on p. 47. The final statement
also uses the bridge to the shared operational LTS from §1 and §2.1.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

/-- The same safety theorem phrased through the reusable LTS reachability API. -/
theorem safety_reachable {Γ : TypeEnv} {before after : Config} {T : Ty}
    (typed : HasType Γ before.expr T)
    (covers : StoreCovers Γ before.store)
    (reachable : lts.CanReach before after) :
    IsValue after.expr ∨ ∃ next, lts.UnlabelledTr after next := sorry

end Silmaril.Courses.Cambridge.L1
