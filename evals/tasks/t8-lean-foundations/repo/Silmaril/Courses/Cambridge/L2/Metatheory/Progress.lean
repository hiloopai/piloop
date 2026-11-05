module

public import Silmaril.Courses.Cambridge.L2.Metatheory.Closedness

/-! # Cambridge L2 progress, Theorem 18

A closed, well-typed expression is a value or can reduce when the store covers
the location assumptions. The closedness premise rules out free variables and
dangling de Bruijn indices. Canonical forms rule out applications of non-lambda
values and primitive operations on values of the wrong type.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

theorem not_stuck {Γ : TypeEnv} {e : Expr} {T : Ty} {s : Store}
    (typing : HasType Γ e T) (closed : e.Closed)
    (covers : StoreCovers Γ s) :
    ¬ Stuck ⟨e, s⟩ := sorry

end Silmaril.Courses.Cambridge.L2
