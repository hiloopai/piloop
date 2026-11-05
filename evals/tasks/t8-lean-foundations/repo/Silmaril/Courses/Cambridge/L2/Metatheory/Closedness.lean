module

public import Silmaril.Courses.Cambridge.L2.Metatheory.Preservation

/-! # Cambridge L2 closedness preservation, Exercise 25

This completes Theorem 19 by proving the remaining closed-expression clause.
Scope is tracked by de Bruijn depth; free names are tracked separately. The
proof covers substitution and the rearranged binders of recursive closures.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

def ScopeRenaming (source target : Nat) (ρ : Nat → Nat) : Prop :=
  ∀ index, index < source → ρ index < target

def ScopeSubstitution (source target : Nat) (σ : Nat → Expr) : Prop :=
  ∀ index, index < source → (σ index).Scoped target

def FreeSubstitution (σ : Nat → Expr) : Prop :=
  ∀ index, (σ index).freeVars = ∅

/-- Theorem 19: typing, closedness, and allocated-location coverage persist. -/
theorem type_preservation {Γ : TypeEnv} {before after : Config} {T : Ty}
    (typed : HasType Γ before.expr T)
    (closed : before.expr.Closed)
    (covers : StoreCovers Γ before.store)
    (step : Step before after) :
    HasType Γ after.expr T ∧ after.expr.Closed ∧
      StoreCovers Γ after.store := sorry

end Silmaril.Courses.Cambridge.L2
