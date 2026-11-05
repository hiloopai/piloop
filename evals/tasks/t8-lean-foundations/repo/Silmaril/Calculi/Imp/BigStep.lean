module

public import Silmaril.Calculi.Imp.Denotation

/-! # Big-step semantics of Imp

The operational semantics of Imp from Part IB Semantics, as a big-step
evaluation relation `Exec c s s'`: running `c` from `s` terminates in `s'`.
Expressions always terminate and have no side effects, so the rules evaluate
them with `A⟦·⟧` and `B⟦·⟧` directly.

§1.2 of the notes claims that the least fixed point `w∞` is "the function
from states to states that we get from the operational semantics". This holds
for every program: `exec_iff_mem_denote` shows `Exec c s s' ↔ s' ∈ C⟦c⟧ s`.
Soundness is an induction on derivations. Completeness for loops goes through
the Kleene chain: `s' ∈ fix F s` iff `s' ∈ Fⁿ(⊥) s` for some `n`, and each
approximation `Fⁿ(⊥)` is sound by induction on `n`.
-/

@[expose] public section

namespace Silmaril.Calculi.Imp


open OmegaCompletePartialOrder Silmaril.Semantics.Domain

variable {L : Type*} [DecidableEq L]

/-- Big-step evaluation of programs (Part IB style). -/
inductive Exec : Prog L → State L → State L → Prop where
  | skip (s : State L) : Exec .skip s s
  | assign (l : L) (a : Aexp L) (s : State L) :
      Exec (.assign l a) s (Function.update s l (A⟦a⟧ s))
  | seq {c₁ c₂ : Prog L} {s s' s'' : State L} :
      Exec c₁ s s' → Exec c₂ s' s'' → Exec (.seq c₁ c₂) s s''
  | iteTrue {b : Bexp L} {c₁ c₂ : Prog L} {s s' : State L} :
      B⟦b⟧ s = true → Exec c₁ s s' → Exec (.ite b c₁ c₂) s s'
  | iteFalse {b : Bexp L} {c₁ c₂ : Prog L} {s s' : State L} :
      B⟦b⟧ s = false → Exec c₂ s s' → Exec (.ite b c₁ c₂) s s'
  | whileFalse {b : Bexp L} {c : Prog L} {s : State L} :
      B⟦b⟧ s = false → Exec (.while b c) s s
  | whileTrue {b : Bexp L} {c : Prog L} {s s' s'' : State L} :
      B⟦b⟧ s = true → Exec c s s' → Exec (.while b c) s' s'' → Exec (.while b c) s s''

end Silmaril.Calculi.Imp
