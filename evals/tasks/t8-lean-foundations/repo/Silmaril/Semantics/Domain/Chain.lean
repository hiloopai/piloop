module

public import Mathlib.Order.Bounds.Basic
public import Mathlib.Order.OmegaCompletePartialOrder

/-! # Chains and least upper bounds

Definition 6 of the notes (§2.3) is Mathlib's `OmegaCompletePartialOrder.Chain`:
a monotone sequence `ℕ →o D`. Least upper bounds are `IsLUB` of the range.
Propositions 2–5 hold in any poset, provided the relevant lubs exist; the
`ωSup` corollaries specialize them to ω-cpos, where every chain has a lub.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D : Type*}

section Poset

variable [PartialOrder D]

/-- Proposition 2: lubs are monotone in the chain. -/
theorem Chain.isLUB_mono {c c' : Chain D} {a b : D} (hle : ∀ n, c n ≤ c' n)
    (ha : IsLUB (Set.range c) a) (hb : IsLUB (Set.range c') b) : a ≤ b := sorry

/-- Drop the first `N` elements of a chain. -/
def Chain.drop (c : Chain D) (N : ℕ) : Chain D :=
  ⟨⟨fun n => c (N + n), fun _ _ h => c.monotone (Nat.add_le_add_left h N)⟩⟩

@[simp] theorem Chain.drop_apply (c : Chain D) (N n : ℕ) : Chain.drop c N n = c (N + n) := sorry

/-- A chain is eventually constant from index `N`. -/
def Chain.EventuallyConstantFrom (c : Chain D) (N : ℕ) : Prop := ∀ n, N ≤ n → c n = c N

/-- Proposition 4: an eventually constant chain has its ultimate value as lub. -/
theorem Chain.isLUB_of_eventuallyConstant {c : Chain D} {N : ℕ}
    (h : Chain.EventuallyConstantFrom c N) : IsLUB (Set.range c) (c N) := sorry

/-- The rows of a doubly indexed monotone family. -/
def Chain.row (d : ℕ →o ℕ →o D) (m : ℕ) : Chain D := ⟨d m⟩

/-- The diagonal of a doubly indexed monotone family. -/
def Chain.diag (d : ℕ →o ℕ →o D) : Chain D :=
  ⟨⟨fun k => d k k, fun _ _ h => (d.monotone h _).trans ((d _).monotone h)⟩⟩

/-- The doubly indexed family with its indices swapped. -/
def Chain.swap (d : ℕ →o ℕ →o D) : ℕ →o ℕ →o D :=
  ⟨fun n => ⟨fun m => d m n, fun _ _ h => d.monotone h n⟩,
    fun _ _ h m => (d m).monotone h⟩

@[simp] theorem Chain.swap_apply (d : ℕ →o ℕ →o D) (m n : ℕ) : Chain.swap d m n = d n m := sorry

/-- Proposition 5, first part: the lubs of the rows form a chain. -/
theorem Chain.row_lub_mono (d : ℕ →o ℕ →o D) {r : ℕ → D}
    (hr : ∀ m, IsLUB (Set.range (Chain.row d m)) (r m)) : Monotone r := sorry

end Poset

section OmegaCPO

variable [OmegaCompletePartialOrder D]

/-- Proposition 4 in an ω-cpo. -/
theorem Chain.ωSup_eq_of_eventuallyConstant {c : Chain D} {N : ℕ}
    (h : Chain.EventuallyConstantFrom c N) : ωSup c = c N := sorry

/-- The chain of lubs of the rows of a doubly indexed family. -/
def Chain.rowSup (d : ℕ →o ℕ →o D) : Chain D :=
  ⟨⟨fun m => ωSup (Chain.row d m),
    Chain.row_lub_mono d (fun m => isLUB_range_ωSup (Chain.row d m))⟩⟩

/-- Proposition 5 in an ω-cpo: the two iterated lubs agree. -/
theorem Chain.ωSup_rowSup_swap (d : ℕ →o ℕ →o D) :
    ωSup (Chain.rowSup d) = ωSup (Chain.rowSup (Chain.swap d)) := sorry

/-- A doubly indexed family is a chain of sequences; Mathlib's (pointwise) `ωSup`
of it in `ℕ → D` is the chain of column lubs. -/
theorem Chain.rowSup_swap_eq_ωSup (d : ℕ →o ℕ →o D) :
    ⇑(Chain.rowSup (Chain.swap d)) =
      ωSup (Chain.map (⟨d⟩ : Chain (ℕ →o D)) OrderHom.coeFnHom) := sorry

end OmegaCPO

end Silmaril.Semantics.Domain
