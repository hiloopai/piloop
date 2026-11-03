module

public import Mathlib.Order.FixedPoints
public import Mathlib.Order.GaloisConnection.Basic

/-! # Monotone dataflow frameworks

A system of dataflow equations over the nodes `ι` of a graph, with values in a
complete lattice `L`, in the shape of the *Optimising Compilers* notes (§2, p. 8
and §3, p. 9; Kildall 1973):

  `x n = transfer n (⨆ m ∈ nbrs n, x m)`.

A backward analysis such as live variables takes `nbrs = succ`; a forward one
such as available expressions takes `nbrs = pred`. A *must* analysis, which
intersects over neighbours and wants the greatest solution, is the same
framework over the order dual `Lᵒᵈ`.

The equations are one monotone operator `Equations.step` on `ι → L`, so by
Knaster–Tarski (`OrderHom.lfp`) they have a least solution, and every *safe*
assignment (a post-fixed point, `step x ≤ x`) lies above it. For a may-analysis
whose soundness argument only uses `step x ≤ x`, every safe assignment, and in
particular the least solution, is sound: this is the notes' "any solution of
these dataflow equations is safe" (p. 8).

`lfp_le_of_galoisConnection` is the fixpoint-transfer theorem of abstract
interpretation (Cousot & Cousot 1977, §7): a Galois connection that commutes
with the two operators up to `≤` maps the concrete least fixed point below the
abstract one. Kleene iteration and its termination on lattices of finite
height are in `Silmaril.Semantics.Dataflow.Iteration`.
-/

@[expose] public section

namespace Silmaril.Semantics.Dataflow

/-- A system of dataflow equations: each node combines the values at its
neighbours with `⨆` and applies its monotone transfer function. -/
structure Equations (ι : Type*) (L : Type*) [CompleteLattice L] where
  /-- The neighbours whose values flow into a node (`succ` for a backward
  analysis, `pred` for a forward one). -/
  nbrs : ι → Set ι
  /-- The transfer function of each node. -/
  transfer : ι → L →o L

namespace Equations

variable {ι L : Type*} [CompleteLattice L] (E : Equations ι L)

/-- The combined value flowing into `n`: `⨆ m ∈ nbrs n, x m` (the
`⋃ s ∈ succ(n), live(s)` of notes p. 8). -/
def join (x : ι → L) (n : ι) : L := ⨆ m ∈ E.nbrs n, x m

theorem join_mono {x y : ι → L} (h : x ≤ y) (n : ι) : E.join x n ≤ E.join y n :=
  iSup₂_mono fun m _ => h m

/-- One round of the equations, as a monotone operator on assignments. -/
def step : (ι → L) →o (ι → L) where
  toFun x n := E.transfer n (E.join x n)
  monotone' _ _ h n := (E.transfer n).monotone (E.join_mono h n)

theorem step_apply (x : ι → L) (n : ι) : E.step x n = E.transfer n (E.join x n) := rfl

/-- `x` solves the equations. -/
def IsSolution (x : ι → L) : Prop := E.step x = x

/-- `x` is a safe assignment: every equation holds as `⊒`. Any solution is
safe, and soundness arguments for may-analyses need only this. -/
def IsSafe (x : ι → L) : Prop := E.step x ≤ x

theorem isSolution_iff (x : ι → L) :
    E.IsSolution x ↔ ∀ n, x n = E.transfer n (E.join x n) :=
  ⟨fun h n => (congrFun h n).symm, fun h => funext fun n => (h n).symm⟩

theorem isSafe_iff (x : ι → L) :
    E.IsSafe x ↔ ∀ n, E.transfer n (E.join x n) ≤ x n := Iff.rfl

theorem IsSolution.isSafe {x : ι → L} (h : E.IsSolution x) : E.IsSafe x := h.le

/-- The least solution (Knaster–Tarski). -/
noncomputable def least : ι → L := E.step.lfp

theorem least_isSolution : E.IsSolution E.least := E.step.map_lfp

theorem least_isSafe : E.IsSafe E.least := E.least_isSolution.isSafe

/-- Every safe assignment lies above the least solution. -/
theorem least_le_of_isSafe {x : ι → L} (h : E.IsSafe x) : E.least ≤ x := E.step.lfp_le h

/-- The least solution is the least of all solutions (notes p. 9: "the least
solution, the one with as few variables as possible live consistent with
safety"). -/
theorem isLeast_least : IsLeast {x | E.IsSolution x} E.least :=
  E.step.isLeast_lfp

end Equations

/-- Fixpoint transfer (Cousot & Cousot 1977, §7): if `l ⊣ u` is a Galois
connection and the abstract operator `f` over-approximates the concrete one
`g` through `l`, then the abstraction of the concrete least fixed point lies
below the abstract least fixed point. -/
theorem lfp_le_of_galoisConnection {α β : Type*} [CompleteLattice α] [CompleteLattice β]
    {l : α → β} {u : β → α} (gc : GaloisConnection l u) (g : α →o α) (f : β →o β)
    (h : ∀ a, l (g a) ≤ f (l a)) : l g.lfp ≤ f.lfp :=
  gc.l_le <| g.lfp_le <| gc.le_u <|
    (h _).trans ((f.monotone (gc.l_u_le _)).trans_eq f.map_lfp)

end Silmaril.Semantics.Dataflow
