module

public import Silmaril.Semantics.Dataflow.Framework
public import Mathlib.Order.OrderIsoNat
public import Mathlib.Order.Iterate

/-! # Kleene iteration for dataflow equations

The notes' algorithm (Optimising Compilers §2, p. 9) starts from the empty
assignment and re-applies the equations "while (live[] changes)". This module
proves the Jacobi form of that algorithm correct:

* `iterate_le_lfp`: every Kleene iterate `f^[k] ⊥` lies below `lfp f`;
* `iterate_eq_lfp_of_isFixedPt`: an iterate that is a fixed point *is* `lfp f`;
* `exists_iterate_eq_lfp`: on a lattice with no infinite ascending chains
  (`WellFoundedGT`, e.g. any finite lattice), some iterate is `lfp f`;
* `iterateUntilStable`: the executable loop with a fuel bound, which returns
  `some x` only when `x` is stable, and then `x = lfp f`.

The in-place (Gauss–Seidel / round-robin) update order actually written in the
notes reaches the same least fixed point; see `Silmaril.Semantics.Dataflow.RoundRobin`.
-/

@[expose] public section

namespace Silmaril.Semantics.Dataflow

variable {α : Type*} [CompleteLattice α]

/-- The Kleene chain `⊥ ≤ f ⊥ ≤ f² ⊥ ≤ …` is monotone. -/
theorem monotone_iterate_bot (f : α →o α) : Monotone fun n => f^[n] ⊥ :=
  Monotone.monotone_iterate_of_le_map f.monotone bot_le

/-- Every Kleene iterate lies below the least fixed point. -/
theorem iterate_le_lfp (f : α →o α) (n : ℕ) : f^[n] ⊥ ≤ f.lfp := by
  induction n with
  | zero => exact bot_le
  | succ n ih =>
    rw [Function.iterate_succ_apply']
    exact (f.monotone ih).trans_eq f.map_lfp

/-- A Kleene iterate that is a fixed point is the least fixed point. -/
theorem iterate_eq_lfp_of_isFixedPt (f : α →o α) {n : ℕ} (h : f (f^[n] ⊥) = f^[n] ⊥) :
    f^[n] ⊥ = f.lfp :=
  le_antisymm (iterate_le_lfp f n) (f.lfp_le_fixed h)

/-- On a lattice without infinite strictly ascending chains, Kleene iteration
from `⊥` reaches the least fixed point after finitely many steps. -/
theorem exists_iterate_eq_lfp [WellFoundedGT α] (f : α →o α) : ∃ n, f^[n] ⊥ = f.lfp := by
  obtain ⟨n, hn⟩ := wellFoundedGT_iff_monotone_chain_condition.1 ‹WellFoundedGT α›
    ⟨fun n => f^[n] ⊥, monotone_iterate_bot f⟩
  refine ⟨n, iterate_eq_lfp_of_isFixedPt f ?_⟩
  have h : f^[n] ⊥ = f^[n + 1] ⊥ := hn (n + 1) (Nat.le_succ n)
  rw [← Function.iterate_succ_apply' f n ⊥]
  exact h.symm

/-- The executable loop: iterate `f` from `x` for at most `fuel` rounds and stop
as soon as the value no longer changes. -/
def iterateUntilStable {β : Type*} [DecidableEq β] (f : β → β) : ℕ → β → Option β
  | 0, _ => none
  | fuel + 1, x => if f x = x then some x else iterateUntilStable f fuel (f x)

/-- A value returned by `iterateUntilStable` is stable. -/
theorem iterateUntilStable_isFixedPt {β : Type*} [DecidableEq β] (f : β → β)
    {fuel : ℕ} {x y : β} (h : iterateUntilStable f fuel x = some y) : f y = y := by
  induction fuel generalizing x with
  | zero => simp [iterateUntilStable] at h
  | succ k ih =>
    simp only [iterateUntilStable] at h
    split_ifs at h with hfx
    · cases h
      exact hfx
    · exact ih h

/-- A value returned by `iterateUntilStable` is an iterate of the start. -/
theorem iterateUntilStable_eq_iterate {β : Type*} [DecidableEq β] (f : β → β)
    {fuel : ℕ} {x y : β} (h : iterateUntilStable f fuel x = some y) :
    ∃ n ≤ fuel, y = f^[n] x := by
  induction fuel generalizing x with
  | zero => simp [iterateUntilStable] at h
  | succ k ih =>
    simp only [iterateUntilStable] at h
    split_ifs at h with hfx
    · cases h
      exact ⟨0, Nat.zero_le _, rfl⟩
    · obtain ⟨n, hn, rfl⟩ := ih h
      exact ⟨n + 1, Nat.succ_le_succ hn, (Function.iterate_succ_apply f n x).symm⟩

/-- If some iterate is stable, the loop with one more round of fuel returns. -/
theorem iterateUntilStable_isSome_of_isFixedPt {β : Type*} [DecidableEq β] (f : β → β)
    {n : ℕ} {x : β} (h : f (f^[n] x) = f^[n] x) : (iterateUntilStable f (n + 1) x).isSome := by
  induction n generalizing x with
  | zero =>
    have h' : f x = x := h
    show (if f x = x then some x else iterateUntilStable f 0 (f x)).isSome = true
    simp [h']
  | succ k ih =>
    show (if f x = x then some x else iterateUntilStable f (k + 1) (f x)).isSome = true
    split_ifs with hfx
    · rfl
    · exact ih (by rwa [Function.iterate_succ_apply] at h)

/-- Started from `⊥`, the loop returns the least fixed point whenever it
returns at all. -/
theorem iterateUntilStable_eq_lfp [DecidableEq α] (f : α →o α) {fuel : ℕ} {y : α}
    (h : iterateUntilStable f fuel ⊥ = some y) : y = f.lfp := by
  have hy := iterateUntilStable_isFixedPt f h
  obtain ⟨n, -, rfl⟩ := iterateUntilStable_eq_iterate f h
  exact iterate_eq_lfp_of_isFixedPt f hy

/-- With enough fuel, the loop started from `⊥` terminates. -/
theorem exists_iterateUntilStable_isSome [DecidableEq α] [WellFoundedGT α] (f : α →o α) :
    ∃ fuel, (iterateUntilStable f fuel ⊥).isSome := by
  obtain ⟨n, hn⟩ := exists_iterate_eq_lfp f
  exact ⟨n + 1, iterateUntilStable_isSome_of_isFixedPt f (by rw [hn]; exact f.map_lfp)⟩

end Silmaril.Semantics.Dataflow
