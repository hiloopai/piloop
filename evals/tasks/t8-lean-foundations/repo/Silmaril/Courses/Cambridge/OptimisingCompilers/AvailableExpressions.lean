module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
public import Silmaril.Semantics.Dataflow.GenKill

/-! # §3 Available expressions (notes pp. 9–10; slides pp. 60–67)

"An expression `e` (typically the RHS of a 3-address instruction) is available
at node `n` if on every path leading to `n` the expression `e` has been
evaluated and not invalidated by an intervening assignment to a variable
occurring in `e`" (notes p. 9). The equations are

  `avail(n) = ⋂ p ∈ pred(n), (avail(p) \ kill(p) ∪ gen(p))`  if `pred(n) ≠ {}`,
  `avail(n) = {}`                                          if `pred(n) = {}`,

and we want their *greatest* solution. This is the must instance of the generic
gen/kill problem `Silmaril.Semantics.Dataflow.GenKill`, i.e. the framework over the order
dual `(Set (Rhs V))ᵒᵈ`, where `⨆` is `⋂` and the least solution is the
greatest one. `gen(x = x+z) = {}` (notes p. 9, footnote 2): an instruction that
overwrites one of its own operands does not make its RHS available.

Main results: `avail_isSolution` (the greatest solution solves the equations)
and `avail_sound` (an available expression is computed, and not killed since,
on every path from a node without predecessors).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

/-- Right-hand sides of 3-address instructions: the expressions of AVAIL. -/
inductive Rhs (V : Type*) where
  | bin (op : AluOp) (a b : Operand V)
  | un (op : UnOp) (a : Operand V)
  deriving DecidableEq

namespace Rhs

variable {V : Type*} [DecidableEq V]

/-- Variables occurring in an expression. -/
def vars : Rhs V → Finset V
  | .bin _ a b => a.vars ∪ b.vars
  | .un _ a => a.vars

/-- The value of an expression. -/
def eval (env : Env V) : Rhs V → ℤ
  | .bin op a b => op.eval (a.eval env) (b.eval env)
  | .un op a => op.eval (a.eval env)

theorem _root_.Silmaril.Courses.Cambridge.OptimisingCompilers.Operand.eval_update_of_not_mem
    {x : V} {o : Operand V} (h : x ∉ o.vars) (v : ℤ) (env : Env V) :
    o.eval (Function.update env x v) = o.eval env := sorry

end Rhs

namespace Instr

variable {V ι : Type*} [DecidableEq V]

/-- The defining assignment `dst := rhs` of an ALU instruction. -/
def assignment? : Instr V ι → Option (V × Rhs V)
  | .alu op d a b _ => some (d, .bin op a b)
  | .alu2 op d a _ => some (d, .un op a)
  | _ => none

/-- `gen(n)`: the expression freshly computed at `n`, unless `n` overwrites one of
its operands (notes p. 9). -/
def gen (i : Instr V ι) : Set (Rhs V) :=
  {e | ∃ d, i.assignment? = some (d, e) ∧ d ∉ e.vars}

/-- `kill(n)`: all expressions containing a variable updated at `n`. -/
def kill (i : Instr V ι) : Set (Rhs V) := {e | ∃ x ∈ i.defs, x ∈ e.vars}

end Instr

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- AVAIL as a forward must gen/kill problem over `pred` (notes p. 9). -/
def availProblem : GenKill ι (Rhs V) where
  nbrs := G.pred
  gen n := (G.code n).gen
  kill n := (G.code n).kill

/-- `avail(n)`: the greatest solution of the AVAIL equations, on entry to `n`
(notes p. 10: "we wish the greatest solution"). -/
noncomputable def avail (n : ι) : Set (Rhs V) := G.availProblem.mustIn n

open Classical in
/-- The greatest solution solves the AVAIL equations of notes p. 9. -/
theorem avail_isSolution (n : ι) :
    G.avail n = if (G.pred n).Nonempty then
      ⋂ p ∈ G.pred n, (G.avail p \ (G.code p).kill ∪ (G.code p).gen) else ∅ := sorry

/-- Soundness of AVAIL, path form (notes p. 9): if `e` is available at `n`, then
along every path `p₀ → ⋯ → pₖ → n` from a node `p₀` without predecessors
(listed from `pₖ` back to `p₀`), some `pᵢ` generates `e` and no later node
kills it. -/
theorem avail_sound {n : ι} {e : Rhs V} (he : e ∈ G.avail n) {p : ι} (hp : p ∈ G.pred n)
    {ps : List ι} (hps : G.availProblem.IsRootedPath (p :: ps)) :
    G.availProblem.HoldsAlong e (p :: ps) := sorry

/-- Semantic soundness: at any configuration reached from an entry
configuration at a node `n`, an available expression `e` was computed earlier
on the run, by a node generating `e`, in a state where it had the value it has
now. -/
theorem avail_sound_semantic {n : ι} {e : Rhs V} (he : e ∈ G.avail n) (hentry : G.pred G.entry = ∅)
    {env : Env V} {mem : Mem} {w : List ℤ} {s : Config V ι}
    (hrun : G.Steps ⟨G.entry, env, mem⟩ w s) (hs : s.pc = n) :
    ∃ (w₁ w₂ : List ℤ) (t : Config V ι), G.Steps ⟨G.entry, env, mem⟩ w₁ t ∧ G.Steps t w₂ s ∧
      e ∈ (G.code t.pc).gen ∧ e.eval t.env = e.eval s.env := sorry

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
