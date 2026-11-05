module

public import Silmaril.Courses.Cambridge.L2.Typing

/-! # Cambridge L2 substitution lemma, Exercise 24

This is Lemma 20 on p. 59 in the locally nameless representation. It proves
the stronger simultaneous-substitution statement, then specializes it to
replacing the nearest bound variable at a beta or `let` redex. The proof also
handles the two binders in a recursive function body.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

def BoundCompatible (Γ Δ : TypeEnv) (ρ : Nat → Nat) : Prop :=
  ∀ index T, Γ.bound[index]? = some T → Δ.bound[ρ index]? = some T

def TypedSubst (Γ Δ : TypeEnv) (σ : Nat → Expr) : Prop :=
  ∀ index T, Γ.bound[index]? = some T → HasType Δ (σ index) T

end Silmaril.Courses.Cambridge.L2
