module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Liveness
public import Mathlib.Data.Fin.VecNotation
public import Mathlib.Tactic.FinCases
public import Mathlib.Data.List.Chain
public import Mathlib.Data.List.Rotate

/-! # §1 Flowgraphs and basic blocks (notes pp. 4–6; slides pp. 42–46)

"A basic block is a maximal sequence of instructions `n₁, …, nₖ` which have
exactly one predecessor (except possibly for `n₁`) [and] exactly one successor
(except possibly for `nₖ`)" (notes p. 6). Liveness can be stored once per block
and recomputed inside it; the block form of the equations (notes p. 9) applies
the instruction transfer functions from the last instruction back to the first.

A flowgraph is in *normal form* when every temporary is assigned at most once
(notes p. 6: "uses a new temporary variable on each occasion that one is
required").
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

/-! ### Chains under a relation that is functional in both directions

The rotation theorem for basic blocks is proved for an abstract relation `R`
(instantiated with the flowgraph edge relation `Flowgraph.Chain`) that is
forward-functional (`R a b → R a c → b = c`) and backward-functional
(`R a c → R b c → a = b`). -/

namespace BlockChain

variable {α : Type*} {R : α → α → Prop}

/-- A non-empty duplicate-free `R`-chain. -/
def Seg (R : α → α → Prop) (l : List α) : Prop := l ≠ [] ∧ l.IsChain R ∧ l.Nodup

/-- A maximal (under `<:+:`) `R`-segment. -/
def MaxSeg (R : α → α → Prop) (l : List α) : Prop := Seg R l ∧ ∀ m, Seg R m → l <:+: m → m = l

end BlockChain

namespace Flowgraph

variable {V ι : Type*} (G : Flowgraph V ι)

/-- `ns` is a path of the flowgraph: each node is followed by a successor. -/
def IsPath : List ι → Prop
  | [] | [_] => True
  | n :: m :: ns => m ∈ G.succ n ∧ IsPath (m :: ns)

/-- The sequence `ns` satisfies the basic-block conditions of notes p. 6: it is a
non-empty path whose interior edges are the only edges out of and into their
endpoints. -/
def IsBlockSegment (ns : List ι) : Prop :=
  ns ≠ [] ∧ G.IsPath ns ∧ ns.Nodup ∧
    ∀ i (h : i + 1 < ns.length),
      G.succ (ns.get ⟨i, by omega⟩) = {ns.get ⟨i + 1, h⟩} ∧
        G.pred (ns.get ⟨i + 1, h⟩) = {ns.get ⟨i, by omega⟩}

/-- A basic block: a block segment that cannot be extended at either end
(notes p. 6, "maximal"). -/
def IsBasicBlock (ns : List ι) : Prop :=
  G.IsBlockSegment ns ∧ ∀ ms, G.IsBlockSegment ms → ns <:+: ms → ms = ns

/-- Every node lies in some basic block. -/
theorem exists_basicBlock_mem [Finite ι] (n : ι) : ∃ ns, G.IsBasicBlock ns ∧ n ∈ ns := sorry

/-- The edge relation inside a basic block: `a`'s only successor is `b` and
`b`'s only predecessor is `a`. -/
def Chain (a b : ι) : Prop := G.succ a = {b} ∧ G.pred b = {a}

/-- Basic blocks sharing a node are rotations of each other, so the blocks
partition the nodes. A rotation is all one can say: on a cycle every rotation
is maximal (`exists_basicBlock_rotation_ne`). -/
theorem basicBlock_isRotated {ns ms : List ι} (hn : G.IsBasicBlock ns) (hm : G.IsBasicBlock ms)
    {n : ι} (h₁ : n ∈ ns) (h₂ : n ∈ ms) : ns ~r ms := sorry

/-- On the two-node cycle `0 → 1 → 0`, both `[0, 1]` and `[1, 0]` are basic
blocks. -/
theorem exists_basicBlock_rotation_ne :
    ∃ (G : Flowgraph Unit (Fin 2)) (ns ms : List (Fin 2)),
      G.IsBasicBlock ns ∧ G.IsBasicBlock ms ∧ 0 ∈ ns ∧ 0 ∈ ms ∧ ns ≠ ms := sorry

variable [DecidableEq V]

/-- The block transfer function of notes p. 9:
`((S \ def(iₖ) ∪ ref(iₖ)) ⋯ \ def(i₁)) ∪ ref(i₁)`. -/
def blockTransfer (ns : List ι) (S : Set V) : Set V :=
  ns.foldr (fun n T => G.liveTransfer n T) S

/-- At a node with a single successor `b`, `live(a)` is the transfer of `live(b)`. -/
theorem live_eq_liveTransfer_of_succ {a b : ι} (h : G.succ a = {b}) :
    G.live a = G.liveTransfer a (G.live b) := sorry

/-- Block-level liveness agrees with node-level liveness: the live set at the
head of a basic block is the transfer, over every node of the block except the
last, of the live-in set `G.live m` of its last node `m` (notes p. 9). -/
theorem live_head_eq_blockTransfer {ns : List ι} (hns : G.IsBasicBlock ns) (n : ι)
    (hn : ns.head? = some n) (m : ι) (hm : ns.getLast? = some m) :
    G.live n = G.blockTransfer ns.dropLast (G.live m) := sorry

/-- Normal form (notes p. 6): each variable in `temps` is defined at most once. -/
def IsNormalForm (temps : Set V) : Prop :=
  ∀ t ∈ temps, ∀ n m, t ∈ (G.code n).defs → t ∈ (G.code m).defs → n = m

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
