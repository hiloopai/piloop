module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress

/-! # Runs of 3-address flowgraphs: unfolding and simulation lemmas

Helper lemmas about the small-step semantics of `ThreeAddress`:

* `step_pc_mem_succ`: a step moves along a flowgraph edge;
* `traces_of_step`, `outcomes_of_step`, `traces_of_halted`,
  `outcomes_of_halted`: one-step unfolding of the observations of a run;
* `obsEquiv_of_step`, `obsEquiv_silent_left`, `obsEquiv_silent_right`,
  `obsEquiv_of_halted`: building observational equivalences step by step,
  used for the worked examples;
* `sim_traces_outcomes`: a lock-step simulation between two flowgraphs (over
  possibly different variables and nodes) preserves traces and outcomes.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

theorem not_halted_of_step {s s' : Config V ι} {o : Option ℤ} (h : G.step s = some (o, s')) :
    ¬ G.Halted s := sorry

theorem exists_step_of_not_halted {s : Config V ι} (h : ¬ G.Halted s) :
    ∃ o s', G.step s = some (o, s') := sorry

variable {G} {V' ι' : Type*} [DecidableEq V'] {G' : Flowgraph V' ι'}

/-! The following lemmas build `traces`/`outcomes` equalities between configurations
of two flowgraphs, possibly over different variables and nodes; for one pair of
types this is `ObsEquiv`. -/

/-! ## Lock-step simulation -/

section Simulation

variable {R : Config V ι → Config V' ι' → Prop}

/-- Hypotheses of a lock-step simulation: related configurations make the same
step, and halt together with the same observations. -/
structure IsLockstep (G : Flowgraph V ι) (G' : Flowgraph V' ι')
    (R : Config V ι → Config V' ι' → Prop) : Prop where
  step : ∀ {s t s' o}, R s t → G.step s = some (o, s') → ∃ t', G'.step t = some (o, t') ∧ R s' t'
  halt : ∀ {s t}, R s t → G.Halted s →
    G'.Halted t ∧ G.resultValues s = G'.resultValues t ∧ s.mem = t.mem

theorem IsLockstep.steps_fwd (hR : IsLockstep G G' R) {s s'' : Config V ι} {t : Config V' ι'}
    {w : List ℤ} (hst : G.Steps s w s'') (h : R s t) :
    ∃ t'', G'.Steps t w t'' ∧ R s'' t'' := sorry

theorem IsLockstep.steps_bwd (hR : IsLockstep G G' R) {s : Config V ι} {t t'' : Config V' ι'}
    {w : List ℤ} (hst : G'.Steps t w t'') (h : R s t) :
    ∃ s'', G.Steps s w s'' ∧ R s'' t'' := sorry

/-- A lock-step simulation preserves traces and outcomes. -/
theorem IsLockstep.traces_outcomes (hR : IsLockstep G G' R) {s : Config V ι}
    {t : Config V' ι'} (h : R s t) :
    G.traces s = G'.traces t ∧ G.outcomes s = G'.outcomes t := sorry

end Simulation

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
