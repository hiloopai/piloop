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

end GenKill

end Silmaril.Semantics.Dataflow
