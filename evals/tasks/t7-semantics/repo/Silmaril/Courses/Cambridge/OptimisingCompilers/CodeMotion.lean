module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.AvailableExpressions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Execution
public import Mathlib.Data.Fin.VecNotation
public import Mathlib.Tactic.FinCases
public import Mathlib.Data.Fintype.Basic

/-! # §6 Code motion and partial redundancy elimination (notes pp. 14–15;
slides pp. 79–83)

"Transformations such as CSE are known collectively as code motion
transformations. Another famous one (more general than CSE) is Partial
Redundancy Elimination" (notes p. 14). An expression is *partially redundant*
at `n` if it is available along some but not all paths into `n`: it is in the
*may* version of AVAIL (`partAvail`) but not in the must version (`avail`).
PRE inserts computations so that partially redundant expressions become fully
redundant, and then CSE removes them.

* `partAvail`: the forward may gen/kill problem with AVAIL's gen and kill;
* `anticipated`: the backward must problem "`e` will be computed on every path
  from `n` before any of its operands change" (very busy expressions), which
  tells PRE where insertion is safe;
* `avail_subset_partAvail`: what is available on all paths is available on some;
* `doWhile` is the notes' example; the marked `a+b` is partially redundant
  (`doWhile_partiallyRedundant`), and hoisting one computation before the loop
  makes it fully redundant (`hoisted_fullyRedundant`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- Partial availability: AVAIL as a may problem. -/
def partAvailProblem : GenKill ι (Rhs V) where
  nbrs := G.pred
  gen n := (G.code n).gen
  kill n := (G.code n).kill

/-- Expressions available on some path into `n`. -/
noncomputable def partAvail (n : ι) : Set (Rhs V) := G.partAvailProblem.mayIn n

/-- `e` is partially redundant at `n`: available on some but not all paths. -/
def PartiallyRedundant (n : ι) (e : Rhs V) : Prop := e ∈ G.partAvail n ∧ e ∉ G.avail n

/-- `e` is fully redundant at `n`, computed there and available. -/
def FullyRedundant (n : ι) (e : Rhs V) : Prop :=
  e ∈ G.avail n ∧ ∃ x, (G.code n).assignment? = some (x, e)

/-- Anticipated (very busy) expressions: a backward must problem over `succ`.
`n` generates the expression it computes (whether or not it overwrites an
operand, since the use precedes the definition) and kills those whose
operands it defines. -/
def anticipatedProblem : GenKill ι (Rhs V) where
  nbrs := G.succ
  gen n := {e | ∃ x, (G.code n).assignment? = some (x, e)}
  kill n := {e | ∃ x ∈ (G.code n).defs, x ∈ e.vars ∧ ¬ ∃ y, (G.code n).assignment? = some (y, e)}

/-- Expressions anticipated on entry to `n`. -/
noncomputable def anticipated (n : ι) : Set (Rhs V) := G.anticipatedProblem.mustOut n

/-- What is available on all rooted paths is available on some. -/
theorem avail_subset_partAvail {n : ι} (hn : ∃ ps, G.availProblem.IsRootedPath (n :: ps)) :
    G.avail n ⊆ G.partAvail n := sorry

end Flowgraph

/-- The notes' PRE example (p. 14), with `a+b` at nodes 2 ("here") and 4.
The notes leave the loop condition `while (...)` unspecified; here it is a
counter `p`, an input that the loop decrements, and the exit returns `c` and
`d`, so that the run's outcome depends on the input and on the computed values:
```
0: a = 1          4: d = a+b
1: b = 2          5: p = p-1
2: c = a+b        6: CMPGT p, #0 → 2 / 7
3: a = 7          7: EXIT [c, d]
```
-/
-- The test depends on input `p`, and the exit exposes `c` and `d`, so the
-- equivalence covers observable terminating cases as well as silent loops.
def doWhile : Flowgraph String (Fin 8) where
  entry := 0
  code := ![.alu2 .mov "a" (.imm 1) 1,
    .alu2 .mov "b" (.imm 2) 2,
    .alu .add "c" (.var "a") (.var "b") 3,
    .alu2 .mov "a" (.imm 7) 4,
    .alu .add "d" (.var "a") (.var "b") 5,
    .alu .sub "p" (.var "p") (.imm 1) 6,
    .cmp .gt (.var "p") (.imm 0) 2 7,
    .exit ["c", "d"]]

/-- The marked `a+b` is partially redundant: computed at node 4 on the back edge,
but not on the path from the entry. -/
theorem doWhile_partiallyRedundant :
    doWhile.PartiallyRedundant 2 (.bin .add (.var "a") (.var "b")) := sorry

/-- The transformed program (notes p. 15), with `e = a+b` hoisted before the
loop as node 2 and the loop body starting at node 3. -/
def doWhileHoisted : Flowgraph String (Fin 9) where
  entry := 0
  code := ![.alu2 .mov "a" (.imm 1) 1,
    .alu2 .mov "b" (.imm 2) 2,
    .alu .add "e" (.var "a") (.var "b") 3,
    .alu .add "c" (.var "a") (.var "b") 4,
    .alu2 .mov "a" (.imm 7) 5,
    .alu .add "d" (.var "a") (.var "b") 6,
    .alu .sub "p" (.var "p") (.imm 1) 7,
    .cmp .gt (.var "p") (.imm 0) 3 8,
    .exit ["c", "d"]]

/-- After hoisting, the marked `a+b` (now node 3) is fully redundant, so CSE
can remove it. -/
theorem hoisted_fullyRedundant :
    doWhileHoisted.FullyRedundant 3 (.bin .add (.var "a") (.var "b")) := sorry

/-- Hoisting preserves behaviour: for every input both programs halt with the
same results (`c = 3` if `p ≤ 1` initially, else `c = 9`; always `d = 9`), the
hoisted `e` being invisible at the exit. -/
theorem doWhileHoisted_equiv (env : Env String) (mem : Mem) :
    doWhile.traces ⟨0, env, mem⟩ = doWhileHoisted.traces ⟨0, env, mem⟩ ∧
      doWhile.outcomes ⟨0, env, mem⟩ = doWhileHoisted.outcomes ⟨0, env, mem⟩ := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers
