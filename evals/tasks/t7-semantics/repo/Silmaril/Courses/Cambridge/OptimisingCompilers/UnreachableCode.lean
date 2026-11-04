module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Runs
public import Silmaril.Semantics.Dataflow.Framework
public import Mathlib.Logic.Relation
public import Mathlib.Data.Fin.VecNotation
public import Mathlib.Tactic.FinCases
public import Mathlib.Data.Fintype.Fin

/-! # §1.2–1.3 Unreachable-code and unreachable-procedure elimination
(notes p. 7; slides pp. 47–53)

*Unreachable-code elimination* (notes §1.2): "mark entry node of each procedure
as reachable; mark every successor of a marked node as reachable and repeat
until no further marks are required". This is the least solution of a forward
dataflow problem over the two-point lattice `Prop`, an instance of
`Silmaril.Semantics.Dataflow.Equations`. The analysis is safe (`reachable_of_steps`):
every node that execution reaches is marked. The converse fails
(`if tautology(x) then C₁ else C₂`, `exists_marked_not_executed`).

The transformation replaces unmarked nodes by `EXIT`; since they are never
executed, behaviour from the entry is unchanged (`eraseUnreachable_obsEquiv`).

*Unreachable-procedure elimination* (notes §1.3) is the same analysis on the
call graph, starting from `main`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

namespace Flowgraph

variable {V ι : Type*} (G : Flowgraph V ι)

/-- The reachability equations: `reach(n) = (n = entry) ∨ ∃ p ∈ pred(n), reach(p)`. -/
def reachEquations : Equations ι Prop where
  nbrs := G.pred
  transfer n :=
    { toFun := fun P => n = G.entry ∨ P
      monotone' := fun _ _ h hp => hp.imp_right h }

/-- The marked (reachable) nodes: the least solution. -/
noncomputable def reachable : ι → Prop := G.reachEquations.least

variable [DecidableEq V]

/-- Safety (notes p. 7): "every node to which execution may flow at execution
will be marked by the algorithm". -/
theorem reachable_of_steps {env : Env V} {mem : Mem} {w : List ℤ} {s : Config V ι}
    (h : G.Steps ⟨G.entry, env, mem⟩ w s) : G.reachable s.pc := sorry

/-- Unreachable-code elimination: replace unmarked nodes by `EXIT`. -/
noncomputable def eraseUnreachable : Flowgraph V ι where
  entry := G.entry
  code n := by
    classical
    exact if G.reachable n then G.code n else .exit []

/-- The transformation preserves behaviour from the entry. -/
theorem eraseUnreachable_obsEquiv (env : Env V) (mem : Mem) :
    G.ObsEquiv G.eraseUnreachable ⟨G.entry, env, mem⟩ ⟨G.entry, env, mem⟩ := sorry

end Flowgraph

/-- The converse of safety fails (notes p. 7, `if tautology(x) then C₁ else C₂`):
a node can be marked but never executed. Here `CMPEQ #0, #0` always branches to
node 1, but node 2 is marked. -/
theorem exists_marked_not_executed :
    ∃ G : Flowgraph Unit (Fin 3), G.reachable 2 ∧
      ∀ env mem w (s : Config Unit (Fin 3)), G.Steps ⟨G.entry, env, mem⟩ w s → s.pc ≠ 2 := sorry

/-! ## §1.3 Unreachable-procedure elimination -/

/-- A call graph (notes p. 7): an edge `(f, g)` whenever `f` contains `CALL g`, or
`CALLI a` where `a` may be `g`. -/
structure CallGraph (P : Type*) where
  main : P
  calls : P → Set P

namespace CallGraph

variable {P : Type*} (C : CallGraph P)

/-- The callability equations, as a forward framework over callers. -/
def callableEquations : Equations P Prop where
  nbrs g := {f | g ∈ C.calls f}
  transfer g :=
    { toFun := fun Q => g = C.main ∨ Q
      monotone' := fun _ _ h hq => hq.imp_right h }

/-- The procedures marked callable. -/
noncomputable def callable : P → Prop := C.callableEquations.least

/-- A call graph is *safe* for a run-time invocation relation if every
invocation is an edge (the notes' over-estimate of `CALLI`). -/
def IsSafeFor (invokes : P → P → Prop) : Prop := ∀ f g, invokes f g → g ∈ C.calls f

/-- Safety (notes p. 7): every procedure invoked during execution, transitively
from `main`, is marked callable. -/
theorem callable_of_invoked {invokes : P → P → Prop} (hC : C.IsSafeFor invokes) {g : P}
    (h : Relation.ReflTransGen invokes C.main g) : C.callable g := sorry

end CallGraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
