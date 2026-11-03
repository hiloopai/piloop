module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress

/-! # Simulations matching a source step by one or two target steps

A generalisation of `Flowgraph.obsEquiv_of_sim` (§1 semantics, notes pp. 4–6)
for transformations that split one instruction into two, such as CSE
(notes p. 13: "replace `n′` with two instructions `n′ : t := a ⊕ b;
n″ : y := t`"). Each source step is matched either by one target step with the
same output, or by a *silent* target step followed by a step with the same
output. The source and target flowgraphs may have different node labels.

The forward direction transports runs of the source. The reverse direction uses
determinism: a target run that stops between the two steps of a matched pair
has produced the same output as the source run stopped before the source step,
because the first target step is silent.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Flowgraph

variable {V ι κ : Type*} [DecidableEq V] (G : Flowgraph V ι) (G' : Flowgraph V κ)
  (R : Config V ι → Config V κ → Prop)

/-- `obsEquiv_of_sim` is the special case where every source step is matched by
a single target step. -/
theorem obsEquiv_of_sim' (G' : Flowgraph V ι) (R : Config V ι → Config V ι → Prop)
    (hfwd : ∀ {s t s' : Config V ι} {o : Option ℤ}, R s t → G.step s = some (o, s') →
      ∃ t', G'.step t = some (o, t') ∧ R s' t')
    (hnone : ∀ {s t : Config V ι}, R s t → G.step s = none → G'.step t = none)
    (hres : ∀ {s t : Config V ι}, R s t → G.Halted s →
      G.resultValues s = G'.resultValues t ∧ s.mem = t.mem)
    {s t : Config V ι} (hst : R s t) : G.ObsEquiv G' s t := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Flowgraph
