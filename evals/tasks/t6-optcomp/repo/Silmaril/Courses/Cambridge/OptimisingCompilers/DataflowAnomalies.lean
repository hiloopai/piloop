module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Liveness
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Runs
public import Mathlib.Data.Fin.VecNotation

/-! # §4 Uses of LVA: data-flow anomalies (notes p. 10; slides p. 69)

"The above warning ['variable `x` may be used before being set'] can be issued
if `x` is live at entry to the procedure" (notes p. 10). If `x` is not live at
the entry, the procedure's behaviour does not depend on the initial value of
`x` (`behaviour_indep_of_not_live_entry`). Because LVA is a safe
over-approximation, some warnings are spurious (slides p. 69, "intentionally
ignoring p!"): `spuriousExample` warns about `x` although no run reads it
uninitialised.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- The warning "variable `x` may be used before it is initialised". -/
def MayBeUninitialised (x : V) : Prop := x ∈ G.live G.entry

/-- Without a warning, the initial value of `x` is irrelevant. -/
theorem behaviour_indep_of_not_live_entry {x : V} (hx : ¬ G.MayBeUninitialised x)
    (env : Env V) (mem : Mem) (v : ℤ) :
    G.ObsEquiv G ⟨G.entry, env, mem⟩ ⟨G.entry, Function.update env x v, mem⟩ := sorry

end Flowgraph

/-- The slide-69 example: `if (p) { x = 42; } … if (p) { print x; }`. -/
def spuriousExample : Flowgraph String (Fin 6) where
  entry := 0
  code := ![.cmp .ne (.var "p") (.imm 0) 1 2,
    .alu2 .mov "x" (.imm 42) 2,
    .cmp .ne (.var "p") (.imm 0) 3 4,
    .print (.var "x") 4,
    .exit [],
    .exit []]

/-- LVA warns about `x` in the slide-69 example … -/
theorem spuriousExample_warns : spuriousExample.MayBeUninitialised "x" := sorry

/-- … but the warning is spurious: the initial value of `x` never matters. -/
theorem spuriousExample_not_semLive : ¬ spuriousExample.SemLive 0 "x" := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers
