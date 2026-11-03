module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress

/-! # Running concrete flowgraphs

Helper lemmas for executing the course's example flowgraphs symbolically
(§1 semantics, notes pp. 4–6): one lemma per instruction form computing its
step, composition of runs, uniqueness of halting runs (the semantics is
deterministic), and the trace and outcome sets of programs without `print`.
`run_steps` executes straight-line code and branches whose condition `simp`
can decide.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Flowgraph

variable {V ι : Type*} [DecidableEq V] {G : Flowgraph V ι} {s s' t : Config V ι}

end Silmaril.Courses.Cambridge.OptimisingCompilers.Flowgraph

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

/-- On a goal `∃ t, G.Steps s [] t ∧ P t`, run straight-line and branch steps,
deciding branch conditions by `simp_all` from the hypotheses in context. -/
syntax "run_steps" : tactic

macro_rules
  | `(tactic| run_steps) => `(tactic| repeat (first
      | refine Flowgraph.exists_steps_silent (Flowgraph.step_alu2 rfl) ?_
      | refine Flowgraph.exists_steps_silent (Flowgraph.step_alu rfl) ?_
      | refine Flowgraph.exists_steps_silent (Flowgraph.step_nop rfl) ?_
      | (refine Flowgraph.exists_steps_silent (Flowgraph.step_cmp_true rfl ?_) ?_
         focus
           simp_all [Function.update_apply, Cond.eval, Operand.eval, UnOp.eval, AluOp.eval]
           done)
      | (refine Flowgraph.exists_steps_silent (Flowgraph.step_cmp_false rfl ?_) ?_
         focus
           simp_all [Function.update_apply, Cond.eval, Operand.eval, UnOp.eval, AluOp.eval]
           done)))

/-- Evaluate environment lookups through updates, operands and operations, using
the hypotheses in context. -/
syntax "flow_simp" : tactic

macro_rules
  | `(tactic| flow_simp) =>
    `(tactic| simp_all [Function.update_apply, Cond.eval, Operand.eval, UnOp.eval, AluOp.eval])

end Silmaril.Courses.Cambridge.OptimisingCompilers
