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

/-- Membership in the least fixed point on a partial-function domain is
membership in some Kleene approximation `fⁿ(⊥)`; see
`Silmaril.Semantics.Domain.partialFun_mem_fix_iff`. -/
theorem mem_fix_iff {X Y : Type*} {f : (X → Part Y) →𝒄 (X → Part Y)} {x : X} {y : Y} :
    y ∈ fix f x ↔ ∃ n, y ∈ f^[n] ⊥ x :=
  Silmaril.Semantics.Domain.partialFun_mem_fix_iff

/-- Soundness: every big-step evaluation is recorded by the denotation. -/
theorem Exec.mem_denote {c : Prog L} {s s' : State L} (h : Exec c s s') : s' ∈ C⟦c⟧ s := by
  induction h with
  | skip => exact Part.mem_some _
  | assign => exact Part.mem_some _
  | seq _ _ ih₁ ih₂ => exact Part.mem_bind_iff.2 ⟨_, ih₁, ih₂⟩
  | iteTrue hb _ ih => simpa [hb] using ih
  | iteFalse hb _ ih => simpa [hb] using ih
  | whileFalse hb =>
      rw [← F_denote_while, F_apply, hb]
      exact Part.mem_some _
  | whileTrue hb _ _ ih₁ ih₂ =>
      rw [← F_denote_while, F_apply, hb]
      exact Part.mem_bind_iff.2 ⟨_, ih₁, ih₂⟩

/-- Completeness for loops: each Kleene approximation of a while loop is
contained in the operational semantics, provided the body is. -/
theorem exec_while_of_mem_kleeneChain {b : Bexp L} {c : Prog L}
    (hc : ∀ s s', s' ∈ C⟦c⟧ s → Exec c s s') (n : ℕ) :
    ∀ s s', s' ∈ (F B⟦b⟧ C⟦c⟧)^[n] ⊥ s → Exec (.while b c) s s' := by
  induction n with
  | zero => exact fun s s' h => absurd h (Part.notMem_none s')
  | succ n ih =>
      intro s s' h
      rw [Function.iterate_succ_apply', F_apply] at h
      cases hb : B⟦b⟧ s
      · rw [hb] at h
        rw [Part.mem_some_iff.1 h]
        exact Exec.whileFalse hb
      · simp only [hb, ↓reduceIte] at h
        obtain ⟨t, ht, h⟩ := Part.mem_bind_iff.1 h
        exact Exec.whileTrue hb (hc s t ht) (ih t s' h)

/-- Completeness: every value of the denotation is reached operationally. -/
theorem exec_of_mem_denote (c : Prog L) {s s' : State L} (h : s' ∈ C⟦c⟧ s) :
    Exec c s s' := by
  induction c generalizing s s' with
  | skip =>
      rw [Part.mem_some_iff.1 h]
      exact Exec.skip s
  | assign l a =>
      rw [Part.mem_some_iff.1 h]
      exact Exec.assign l a s
  | seq c₁ c₂ ih₁ ih₂ =>
      obtain ⟨t, ht, h⟩ := Part.mem_bind_iff.1 h
      exact Exec.seq (ih₁ ht) (ih₂ h)
  | ite b c₁ c₂ ih₁ ih₂ =>
      rw [denote_ite] at h
      cases hb : B⟦b⟧ s
      · rw [hb] at h
        exact Exec.iteFalse hb (ih₂ h)
      · simp only [hb, ↓reduceIte] at h
        exact Exec.iteTrue hb (ih₁ h)
  | «while» b c ih =>
      obtain ⟨n, hn⟩ := mem_fix_iff.1 h
      exact exec_while_of_mem_kleeneChain (fun _ _ => ih) n s s' hn

/-- §1.2: the denotational and operational semantics agree. -/
theorem exec_iff_mem_denote {c : Prog L} {s s' : State L} : Exec c s s' ↔ s' ∈ C⟦c⟧ s :=
  ⟨Exec.mem_denote, exec_of_mem_denote c⟩

end Silmaril.Calculi.Imp
