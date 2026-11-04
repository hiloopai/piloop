module

public import Silmaril.Semantics.Dataflow.Framework
public import Mathlib.Order.CompleteBooleanAlgebra

/-! # Gen/kill (bit-vector) problems

Most analyses of Part A of *Optimising Compilers* are gen/kill problems: the
value leaving a node is `in(n) \ kill(n) ∪ gen(n)`, and `in(n)` combines the
values leaving the neighbours, by `⋃` for a *may* problem (live variables,
reaching definitions, partial availability) and by `⋂` for a *must* problem
(available expressions, notes §3, p. 9, where a node without predecessors has
`in(n) = {}`).

Both are instances of `Equations` on the values leaving each node: the may
problem over `Set α`, the must problem over the dual `(Set α)ᵒᵈ`, so that its
least solution in the framework is the greatest solution in `Set α`.
-/

@[expose] public section

namespace Silmaril.Semantics.Dataflow

/-- A gen/kill problem over neighbours `nbrs` (`pred` for forward problems,
`succ` for backward ones). -/
structure GenKill (ι : Type*) (α : Type*) where
  nbrs : ι → Set ι
  gen : ι → Set α
  kill : ι → Set α

namespace GenKill

variable {ι α : Type*} (K : GenKill ι α)

/-- The may problem: `out(n) = (⋃ m ∈ nbrs n, out(m)) \ kill(n) ∪ gen(n)`. -/
def mayEquations : Equations ι (Set α) where
  nbrs := K.nbrs
  transfer n :=
    { toFun := fun S => S \ K.kill n ∪ K.gen n
      monotone' := fun _ _ h => Set.union_subset_union_left _ (Set.sdiff_subset_sdiff_left h) }

open Classical in
/-- The must problem: `out(n) = (⋂ m ∈ nbrs n, out(m)) \ kill(n) ∪ gen(n)`, with
`{}` in place of the intersection when `n` has no neighbours. -/
noncomputable def mustEquations : Equations ι (Set α)ᵒᵈ where
  nbrs := K.nbrs
  transfer n :=
    { toFun := fun S => OrderDual.toDual
        ((if (K.nbrs n).Nonempty then OrderDual.ofDual S else ∅) \ K.kill n ∪ K.gen n)
      monotone' := fun S S' h => by
        refine OrderDual.toDual_le_toDual.2 ?_
        refine Set.union_subset_union_left _ (Set.sdiff_subset_sdiff_left ?_)
        split_ifs
        · exact h
        · exact le_rfl }

/-- The least solution of the may problem (values leaving each node). -/
noncomputable def mayOut : ι → Set α := K.mayEquations.least

/-- The greatest solution of the must problem (values leaving each node). -/
noncomputable def mustOut (n : ι) : Set α := OrderDual.ofDual (K.mustEquations.least n)

open Classical in
/-- The must value entering a node: `⋂ m ∈ nbrs n, out(m)`, or `{}` without
neighbours. -/
noncomputable def mustIn (n : ι) : Set α :=
  if (K.nbrs n).Nonempty then ⋂ m ∈ K.nbrs n, K.mustOut m else ∅

/-- The may value entering a node. -/
def mayIn (n : ι) : Set α := ⋃ m ∈ K.nbrs n, K.mayOut m

/-- The greatest solution solves the must equations. -/
theorem mustOut_eq (n : ι) : K.mustOut n = K.mustIn n \ K.kill n ∪ K.gen n :=
  congrArg OrderDual.ofDual (congrFun K.mustEquations.least_isSolution n).symm

open Classical in
/-- Every post-solution of the must equations lies below the greatest one. -/
theorem le_mustOut {x : ι → Set α}
    (hx : ∀ n, x n ⊆
      (if (K.nbrs n).Nonempty then ⋂ m ∈ K.nbrs n, x m else ∅) \ K.kill n ∪ K.gen n)
    (n : ι) : x n ⊆ K.mustOut n := by
  have hsafe : K.mustEquations.IsSafe fun n => OrderDual.toDual (x n) :=
    (Equations.isSafe_iff _ _).2 fun n => hx n
  exact K.mustEquations.least_le_of_isSafe hsafe n

/-- The least solution solves the may equations. -/
theorem mayOut_eq (n : ι) : K.mayOut n = K.mayIn n \ K.kill n ∪ K.gen n :=
  (congrFun K.mayEquations.least_isSolution n).symm

/-- `ps`, read from its head backwards along `nbrs`, is a path ending at a node
without neighbours (a *rooted* path into its head). -/
def IsRootedPath : List ι → Prop
  | [] => False
  | [m] => K.nbrs m = ∅
  | n :: m :: ps => m ∈ K.nbrs n ∧ IsRootedPath (m :: ps)

/-- The must property along a rooted path (listed from its head): some node
generates `a` and no node after it kills `a`. -/
def HoldsAlong (a : α) : List ι → Prop
  | [] => False
  | n :: ps => a ∈ K.gen n ∨ (a ∉ K.kill n ∧ HoldsAlong a ps)

/-- Soundness of the must problem: a must fact holds along every rooted path. -/
theorem mustOut_holdsAlong {n : ι} {a : α} (ha : a ∈ K.mustOut n) {ps : List ι}
    (hps : K.IsRootedPath (n :: ps)) : K.HoldsAlong a (n :: ps) := by
  induction ps generalizing n with
  | nil =>
    simp only [IsRootedPath] at hps
    have hn : ¬ (K.nbrs n).Nonempty := by rw [hps]; exact Set.not_nonempty_empty
    rw [K.mustOut_eq, mustIn] at ha
    simp only [hn, ↓reduceIte, Set.empty_sdiff, Set.empty_union] at ha
    exact Or.inl ha
  | cons m ps ih =>
    simp only [IsRootedPath] at hps
    obtain ⟨hm, hps⟩ := hps
    rw [K.mustOut_eq] at ha
    rcases ha with ⟨ha, hk⟩ | ha
    · have hne : (K.nbrs n).Nonempty := ⟨m, hm⟩
      simp only [mustIn, hne, ↓reduceIte, Set.mem_iInter₂] at ha
      exact Or.inr ⟨hk, ih (ha m hm) hps⟩
    · exact Or.inl ha

/-- Must values are may values at nodes with a rooted path. Without one the
inclusion can fail: on a lone self-loop with empty gen and kill, the greatest
must solution is everything and the least may solution is empty. -/
theorem mustOut_subset_mayOut {n : ι} (hn : ∃ ps, K.IsRootedPath (n :: ps)) :
    K.mustOut n ⊆ K.mayOut n := by
  obtain ⟨ps, hps⟩ := hn
  intro a ha
  induction ps generalizing n with
  | nil =>
    simp only [IsRootedPath] at hps
    have hn : ¬ (K.nbrs n).Nonempty := by rw [hps]; exact Set.not_nonempty_empty
    rw [K.mustOut_eq, mustIn] at ha
    simp only [hn, ↓reduceIte, Set.empty_sdiff, Set.empty_union] at ha
    rw [K.mayOut_eq]
    exact Or.inr ha
  | cons m ps ih =>
    simp only [IsRootedPath] at hps
    obtain ⟨hm, hps⟩ := hps
    rw [K.mustOut_eq] at ha
    rw [K.mayOut_eq]
    rcases ha with ⟨ha, hk⟩ | ha
    · have hne : (K.nbrs n).Nonempty := ⟨m, hm⟩
      simp only [mustIn, hne, ↓reduceIte, Set.mem_iInter₂] at ha
      exact Or.inl ⟨Set.mem_iUnion₂.2 ⟨m, hm, ih hps (ha m hm)⟩, hk⟩
    · exact Or.inr ha

end GenKill

end Silmaril.Semantics.Dataflow
