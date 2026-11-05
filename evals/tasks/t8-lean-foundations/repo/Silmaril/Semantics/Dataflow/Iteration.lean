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

/-- Every Kleene iterate lies below the least fixed point. -/
theorem iterate_le_lfp (f : α →o α) (n : ℕ) : f^[n] ⊥ ≤ f.lfp := sorry

/-- A Kleene iterate that is a fixed point is the least fixed point. -/
theorem iterate_eq_lfp_of_isFixedPt (f : α →o α) {n : ℕ} (h : f (f^[n] ⊥) = f^[n] ⊥) :
    f^[n] ⊥ = f.lfp := sorry

/-- The executable loop: iterate `f` from `x` for at most `fuel` rounds and stop
as soon as the value no longer changes. -/
def iterateUntilStable {β : Type*} [DecidableEq β] (f : β → β) : ℕ → β → Option β
  | 0, _ => none
  | fuel + 1, x => if f x = x then some x else iterateUntilStable f fuel (f x)

/-- A value returned by `iterateUntilStable` is stable. -/
theorem iterateUntilStable_isFixedPt {β : Type*} [DecidableEq β] (f : β → β)
    {fuel : ℕ} {x y : β} (h : iterateUntilStable f fuel x = some y) : f y = y := sorry

/-- A value returned by `iterateUntilStable` is an iterate of the start. -/
theorem iterateUntilStable_eq_iterate {β : Type*} [DecidableEq β] (f : β → β)
    {fuel : ℕ} {x y : β} (h : iterateUntilStable f fuel x = some y) :
    ∃ n ≤ fuel, y = f^[n] x := sorry

/-- If some iterate is stable, the loop with one more round of fuel returns. -/
theorem iterateUntilStable_isSome_of_isFixedPt {β : Type*} [DecidableEq β] (f : β → β)
    {n : ℕ} {x : β} (h : f (f^[n] x) = f^[n] x) : (iterateUntilStable f (n + 1) x).isSome := sorry

/-- Started from `⊥`, the loop returns the least fixed point whenever it
returns at all. -/
theorem iterateUntilStable_eq_lfp [DecidableEq α] (f : α →o α) {fuel : ℕ} {y : α}
    (h : iterateUntilStable f fuel ⊥ = some y) : y = f.lfp := sorry

/-- With enough fuel, the loop started from `⊥` terminates. -/
theorem exists_iterateUntilStable_isSome [DecidableEq α] [WellFoundedGT α] (f : α →o α) :
    ∃ fuel, (iterateUntilStable f fuel ⊥).isSome := sorry

end Silmaril.Semantics.Dataflow
