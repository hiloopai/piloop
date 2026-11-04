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

/-- Remark 1 (iv): the lub of a chain is unique; the chain-range instance of
Mathlib's `IsLUB.unique`. -/
theorem Chain.isLUB_unique {c : Chain D} {a b : D} (ha : IsLUB (Set.range c) a)
    (hb : IsLUB (Set.range c) b) : a = b := IsLUB.unique ha hb

/-- Proposition 2: lubs are monotone in the chain. -/
theorem Chain.isLUB_mono {c c' : Chain D} {a b : D} (hle : ∀ n, c n ≤ c' n)
    (ha : IsLUB (Set.range c) a) (hb : IsLUB (Set.range c') b) : a ≤ b :=
  ha.2 <| by
    rintro _ ⟨n, rfl⟩
    exact (hle n).trans (hb.1 ⟨n, rfl⟩)

/-- Drop the first `N` elements of a chain. -/
def Chain.drop (c : Chain D) (N : ℕ) : Chain D :=
  ⟨⟨fun n => c (N + n), fun _ _ h => c.monotone (Nat.add_le_add_left h N)⟩⟩

@[simp] theorem Chain.drop_apply (c : Chain D) (N n : ℕ) : Chain.drop c N n = c (N + n) := rfl

/-- Discarding an initial segment does not change the upper bounds of a chain. -/
theorem Chain.upperBounds_drop (c : Chain D) (N : ℕ) :
    upperBounds (Set.range (Chain.drop c N)) = upperBounds (Set.range c) := by
  ext x
  constructor
  · rintro hx _ ⟨n, rfl⟩
    exact (c.monotone (Nat.le_add_left n N)).trans (hx ⟨n, rfl⟩)
  · rintro hx _ ⟨n, rfl⟩
    exact hx ⟨N + n, rfl⟩

/-- Proposition 3: discarding an initial segment does not change the lub. -/
theorem Chain.isLUB_drop_iff (c : Chain D) (N : ℕ) {a : D} :
    IsLUB (Set.range (Chain.drop c N)) a ↔ IsLUB (Set.range c) a := by
  simp only [IsLUB, IsLeast, lowerBounds, Chain.upperBounds_drop]

/-- A chain is eventually constant from index `N`. -/
def Chain.EventuallyConstantFrom (c : Chain D) (N : ℕ) : Prop := ∀ n, N ≤ n → c n = c N

/-- Proposition 4: an eventually constant chain has its ultimate value as lub. -/
theorem Chain.isLUB_of_eventuallyConstant {c : Chain D} {N : ℕ}
    (h : Chain.EventuallyConstantFrom c N) : IsLUB (Set.range c) (c N) := by
  refine ⟨?_, fun x hx => hx ⟨N, rfl⟩⟩
  rintro _ ⟨n, rfl⟩
  rcases le_total n N with hn | hn
  · exact c.monotone hn
  · exact (h n hn).le

/-- The rows of a doubly indexed monotone family. -/
def Chain.row (d : ℕ →o ℕ →o D) (m : ℕ) : Chain D := ⟨d m⟩

/-- The diagonal of a doubly indexed monotone family. -/
def Chain.diag (d : ℕ →o ℕ →o D) : Chain D :=
  ⟨⟨fun k => d k k, fun _ _ h => (d.monotone h _).trans ((d _).monotone h)⟩⟩

/-- The doubly indexed family with its indices swapped. -/
def Chain.swap (d : ℕ →o ℕ →o D) : ℕ →o ℕ →o D :=
  ⟨fun n => ⟨fun m => d m n, fun _ _ h => d.monotone h n⟩,
    fun _ _ h m => (d m).monotone h⟩

@[simp] theorem Chain.swap_apply (d : ℕ →o ℕ →o D) (m n : ℕ) : Chain.swap d m n = d n m := rfl

/-- Proposition 5, first part: the lubs of the rows form a chain. -/
theorem Chain.row_lub_mono (d : ℕ →o ℕ →o D) {r : ℕ → D}
    (hr : ∀ m, IsLUB (Set.range (Chain.row d m)) (r m)) : Monotone r :=
  fun _ _ h => Chain.isLUB_mono (fun n => d.monotone h n) (hr _) (hr _)

/-- Proposition 5 (diagonalisation): the lub of the lubs of the rows is the lub
of the diagonal. -/
theorem Chain.isLUB_diag (d : ℕ →o ℕ →o D) {r : ℕ → D} {δ : D}
    (hr : ∀ m, IsLUB (Set.range (Chain.row d m)) (r m))
    (hδ : IsLUB (Set.range (Chain.diag d)) δ) : IsLUB (Set.range r) δ := by
  constructor
  · rintro _ ⟨m, rfl⟩
    refine (hr m).2 ?_
    rintro _ ⟨n, rfl⟩
    have hmn : d m n ≤ d (max m n) (max m n) :=
      (d.monotone (le_max_left m n) n).trans ((d _).monotone (le_max_right m n))
    exact hmn.trans (hδ.1 ⟨max m n, rfl⟩)
  · intro x hx
    refine hδ.2 ?_
    rintro _ ⟨k, rfl⟩
    exact ((hr k).1 ⟨k, rfl⟩).trans (hx ⟨k, rfl⟩)

end Poset

section OmegaCPO

variable [OmegaCompletePartialOrder D]

/-- Proposition 3 in an ω-cpo. -/
@[simp] theorem Chain.ωSup_drop (c : Chain D) (N : ℕ) : ωSup (Chain.drop c N) = ωSup c :=
  IsLUB.unique (isLUB_range_ωSup _) ((Chain.isLUB_drop_iff c N).2 (isLUB_range_ωSup c))

/-- Proposition 4 in an ω-cpo. -/
theorem Chain.ωSup_eq_of_eventuallyConstant {c : Chain D} {N : ℕ}
    (h : Chain.EventuallyConstantFrom c N) : ωSup c = c N :=
  IsLUB.unique (isLUB_range_ωSup c) (Chain.isLUB_of_eventuallyConstant h)

/-- The chain of lubs of the rows of a doubly indexed family. -/
def Chain.rowSup (d : ℕ →o ℕ →o D) : Chain D :=
  ⟨⟨fun m => ωSup (Chain.row d m),
    Chain.row_lub_mono d (fun m => isLUB_range_ωSup (Chain.row d m))⟩⟩

/-- Proposition 5 in an ω-cpo: `⨆ₘ ⨆ₙ dₘₙ = ⨆ₖ dₖₖ`. -/
theorem Chain.ωSup_rowSup (d : ℕ →o ℕ →o D) : ωSup (Chain.rowSup d) = ωSup (Chain.diag d) :=
  (IsLUB.unique (Chain.isLUB_diag d (fun m => isLUB_range_ωSup (Chain.row d m))
    (isLUB_range_ωSup _)) (isLUB_range_ωSup (Chain.rowSup d))).symm

/-- Proposition 5 in an ω-cpo: the two iterated lubs agree. -/
theorem Chain.ωSup_rowSup_swap (d : ℕ →o ℕ →o D) :
    ωSup (Chain.rowSup d) = ωSup (Chain.rowSup (Chain.swap d)) := by
  rw [Chain.ωSup_rowSup, Chain.ωSup_rowSup]
  rfl

/-- A doubly indexed family is a chain of sequences; Mathlib's (pointwise) `ωSup`
of it in `ℕ → D` is the chain of column lubs. -/
theorem Chain.rowSup_swap_eq_ωSup (d : ℕ →o ℕ →o D) :
    ⇑(Chain.rowSup (Chain.swap d)) =
      ωSup (Chain.map (⟨d⟩ : Chain (ℕ →o D)) OrderHom.coeFnHom) :=
  rfl

end OmegaCPO

end Silmaril.Semantics.Domain
