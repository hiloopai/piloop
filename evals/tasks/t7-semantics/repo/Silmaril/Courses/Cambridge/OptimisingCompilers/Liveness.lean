module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
public import Silmaril.Semantics.Dataflow.Iteration
public import Mathlib.Data.Fintype.Pi
public import Mathlib.Algebra.Order.BigOperators.Group.Finset

/-! # §2 Live variable analysis (notes pp. 8–9; slides pp. 54–59)

* `SynLive` is *syntactic liveness* (notes p. 8): `x` is syntactically live at
  `n` if there is a path from `n` to a node referencing `x` along which `x` is
  not defined.
* `SemLive` is *semantic liveness* (notes p. 8): changing the value of `x` at
  `n` can change the I/O behaviour of some execution from `n`.
* `liveEquations` are the dataflow equations
  `live(n) = (⋃ s ∈ succ(n), live(s)) \ def(n) ∪ ref(n)` (notes p. 8), an
  instance of the generic framework `Silmaril.Semantics.Dataflow.Equations`, and `live`
  is their least solution.

Main results:

* `mem_live_iff_synLive`: the least solution is exactly syntactic liveness;
* `semLive_subset_synLive`: `sem-live(n) ⊆ syn-live(n)` (notes p. 8), and more
  generally `semLive_subset_of_isSafe`: any safe assignment over-approximates
  semantic liveness ("any solution of these dataflow equations is safe", p. 8);
* `agree_step`: the simulation lemma behind both, also used by dead-code
  elimination: configurations agreeing on the live variables step together;
* `lva`: the executable algorithm (p. 9) on finite flowgraphs, with
  `lva_isSafe` and `lva_eq_live` (it computes the least solution).

Ambiguous references through pointers (p. 9) do not arise: memory is separate
from the variables, and `LDI`/`STI` only reference their operand variables.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- The liveness transfer function `S ↦ S \ def(n) ∪ ref(n)` (notes p. 8). -/
def liveTransfer (n : ι) : Set V →o Set V where
  toFun S := S \ ↑(G.code n).defs ∪ ↑(G.code n).refs
  monotone' _ _ h := Set.union_subset_union_left _ (Set.sdiff_subset_sdiff_left h)

/-- The LVA dataflow equations: a backward framework over `succ` (notes p. 8). -/
def liveEquations : Equations ι (Set V) where
  nbrs := G.succ
  transfer := G.liveTransfer

/-- `live(n)`: the least solution of the LVA equations (notes p. 9). -/
noncomputable def live : ι → Set V := G.liveEquations.least

/-- `out-live(n) = ⋃ s ∈ succ(n), live(s)` (notes p. 8). -/
def outLive (live : ι → Set V) (n : ι) : Set V := ⋃ s ∈ G.succ n, live s

/-- The two-stage form of the equations (notes p. 8):
`in-live(n) = out-live(n) \ def(n) ∪ ref(n)`. -/
theorem liveEquations_step_apply (x : ι → Set V) (n : ι) :
    G.liveEquations.step x n = G.outLive x n \ ↑(G.code n).defs ∪ ↑(G.code n).refs := sorry

/-- A safe assignment contains the references of each node. -/
theorem refs_subset_of_isSafe {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    (n : ι) : (↑(G.code n).refs : Set V) ⊆ live n := sorry

/-- A safe assignment propagates liveness backwards across non-defining nodes. -/
theorem mem_of_isSafe_succ {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {n m : ι} (hm : m ∈ G.succ n) {x : V} (hx : x ∈ live m) (hd : x ∉ (G.code n).defs) :
    x ∈ live n := sorry

/-- Syntactic liveness (notes p. 8): a path from `n` to a node referencing `x`
that contains no definition of `x` (before the reference). -/
inductive SynLive : ι → V → Prop
  | ref {n : ι} {x : V} : x ∈ (G.code n).refs → SynLive n x
  | succ {n m : ι} {x : V} : m ∈ G.succ n → x ∉ (G.code n).defs → SynLive m x → SynLive n x

/-- Configurations that agree on the variables of `S`. -/
def AgreeOn (S : Set V) (s t : Config V ι) : Prop :=
  s.pc = t.pc ∧ s.mem = t.mem ∧ ∀ x ∈ S, s.env x = t.env x

/-- The simulation lemma: if `live` is safe and two configurations agree on
`live(pc)`, then they step to configurations agreeing on `live(pc')`, with
the same output. -/
theorem agree_step {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {s t s' : Config V ι} {o : Option ℤ}
    (hst : AgreeOn (live s.pc) s t) (hs : G.step s = some (o, s')) :
    ∃ t', G.step t = some (o, t') ∧ AgreeOn (live s'.pc) s' t' := sorry

/-- At an `EXIT` reached in agreement, two flowgraphs with the same exit return
the same result values. -/
theorem resultValues_eq_of_agree {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {G' : Flowgraph V ι} {s t : Config V ι} (hst : AgreeOn (live s.pc) s t) {rs : List V}
    (hrs : G.code s.pc = .exit rs) (hrs' : G'.code t.pc = .exit rs) :
    G.resultValues s = G'.resultValues t := sorry

/-- Semantic liveness (notes p. 8): there is an execution starting at `n` whose
I/O behaviour is changed by changing the value of `x`. -/
def SemLive (n : ι) (x : V) : Prop :=
  ∃ (env : Env V) (mem : Mem) (v : ℤ),
    ¬ G.ObsEquiv G ⟨n, env, mem⟩ ⟨n, Function.update env x v, mem⟩

/-- `sem-live(n) ⊆ syn-live(n)` (notes p. 8). -/
theorem semLive_subset_synLive {n : ι} {x : V} (h : G.SemLive n x) : G.SynLive n x := sorry

/-! ## The algorithm (notes p. 9) -/

section Algorithm

variable [Fintype ι] [DecidableEq ι]

/-- One round of the LVA equations on finite sets. -/
def liveStep (x : ι → Finset V) (n : ι) : Finset V :=
  ((G.code n).succs.toFinset.biUnion x \ (G.code n).defs) ∪ (G.code n).refs

/-- The fuel bound: the lattice of assignments below `G.vars` has height
`card ι * card G.vars`. -/
def lvaFuel : ℕ := Fintype.card ι * G.vars.card + 1

/-- The LVA algorithm (notes p. 9): start from `{}` everywhere and re-apply the
equations until nothing changes. If the fuel runs out (it never does,
`lva_converges`), fall back on the safe assignment `G.vars` everywhere. -/
def lva : ι → Finset V :=
  (iterateUntilStable G.liveStep G.lvaFuel ⊥).getD fun _ => G.vars

set_option linter.unusedSectionVars false in
/-- `liveStep` is the set form of the equations. -/
theorem coe_liveStep (x : ι → Finset V) :
    (fun n => (G.liveStep x n : Set V)) = G.liveEquations.step fun n => (x n : Set V) := sorry

omit [Fintype ι] in
theorem liveStep_mono : Monotone G.liveStep := sorry

theorem liveStep_subset_vars {x : ι → Finset V} (hx : ∀ n, x n ⊆ G.vars) (n : ι) :
    G.liveStep x n ⊆ G.vars := sorry

theorem iterate_liveStep_subset_vars (k : ℕ) (n : ι) : (G.liveStep^[k] ⊥) n ⊆ G.vars := sorry

omit [DecidableEq V] [DecidableEq ι] in
theorem sum_card_lt_of_lt {x y : ι → Finset V} (h : x ≤ y) (hne : x ≠ y) :
    ∑ n, (x n).card < ∑ n, (y n).card := sorry

/-- The loop terminates within `lvaFuel` rounds. -/
theorem lva_converges : (iterateUntilStable G.liveStep G.lvaFuel ⊥).isSome := sorry

/-- The Finset iterates are the set iterates of the LVA equations. -/
theorem coe_iterate_liveStep (k : ℕ) :
    (fun n => ((G.liveStep^[k] ⊥) n : Set V)) = G.liveEquations.step^[k] ⊥ := sorry

theorem coe_lva_eq_live : (fun n => (G.lva n : Set V)) = G.live := sorry

/-- The algorithm's result is safe. -/
theorem lva_isSafe : G.liveEquations.IsSafe fun n => (G.lva n : Set V) := sorry

/-- The algorithm computes the least solution (notes p. 9: "it always
terminates with the least solution"). -/
theorem lva_eq_live : (fun n => (G.lva n : Set V)) = G.live := sorry

end Algorithm

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
