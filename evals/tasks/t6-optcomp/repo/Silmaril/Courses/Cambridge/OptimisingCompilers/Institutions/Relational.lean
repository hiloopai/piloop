module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Transitions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.DeadCode

/-! # Liveness and dead-code elimination in the relational institution

Liveness is not a property of one state but of *pairs* of executions: `x` is
dead at `n` when changing `x` at `n` cannot change the I/O behaviour (semantic
liveness, notes p. 8). Its sentences therefore live in a relational (2-safety)
institution whose models are pairs of transitions.

* Signatures: register files `RegSig`; node labels `ι` fixed.
* Models over `V`: pairs of transitions `Trans V ι × Trans V ι`.
* Sentences `RSen V ι`:
  - `left φ`, `right φ`: a transition sentence about one side;
  - `lockstep n S T`: if the left step starts at `n` and both sources agree on
    `S` (same node, same memory, same values of `S`), then the steps have the
    same output and their targets agree on `T` at the left target's node.
* The satisfaction condition (`RSen.holds_rename`) holds along every renaming.

`transToRel` is the comorphism from the transition institution embedding a
sentence on the left.

**Analyses as theories.** A liveness assignment `live` gives the theory
`liveAxioms live = {lockstep n (live n) live | n}`: "agreement on the live
variables is preserved by a step". The *product theory* `pairAxioms G G'` puts
the theory of `G` on the left and that of `G'` on the right. Then:

* `liveAxioms_entailed`: a safe liveness assignment's theory is a consequence of
  the product theory of `G` with itself; this is the simulation lemma
  `Flowgraph.agree_step` of the course spine, read through the transition
  institution;
* `dceWith_liveAxioms_entailed`: the same theory is a consequence of the
  product of `G` with its dead-code-eliminated version `G.dceWith live`, which
  is `Flowgraph.dceWith_agree_step` (slide p. 68);
* `exists_entailed_not_isSafe`: the converse fails. Entailment of the liveness
  theory is a *semantic* notion, strictly weaker than the dataflow safety
  `IsSafe` (notes p. 8: "sem-live(n) ⊆ syn-live(n)"): `x := x` does not need
  `x` live.

The observational consequences (`dce_obsEquiv`) are in
`Institutions.Observational`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Logic.Institution

namespace Institutions

/-- The sentences of the relational institution. -/
inductive RSen (V ι : Type) : Type where
  /-- A sentence about the left transition. -/
  | left (φ : TSen V ι)
  /-- A sentence about the right transition. -/
  | right (φ : TSen V ι)
  /-- Steps from `n` in sources agreeing on `S` have equal outputs and targets
  agreeing on `T` at the left target's node. -/
  | lockstep (n : ι) (S : Set V) (T : ι → Set V)

namespace RSen

variable {V R W ι : Type}

/-- Rename the registers of a relational sentence. -/
def rename (σ : V → R) : RSen V ι → RSen R ι
  | .left φ => .left (φ.rename σ)
  | .right φ => .right (φ.rename σ)
  | .lockstep n S T => .lockstep n (σ '' S) (fun m => σ '' T m)

variable [DecidableEq V] [DecidableEq R]

/-- Satisfaction of a relational sentence by a pair of transitions. -/
def Holds (p : Trans V ι × Trans V ι) : RSen V ι → Prop
  | .left φ => φ.Holds p.1
  | .right φ => φ.Holds p.2
  | .lockstep n S T => p.1.src.pc = n → Flowgraph.AgreeOn S p.1.src p.2.src →
      p.1.out = p.2.out ∧ Flowgraph.AgreeOn (T p.1.dst.pc) p.1.dst p.2.dst

theorem rename_id (φ : RSen V ι) : φ.rename id = φ := by
  cases φ <;> simp [rename, TSen.rename_id]

theorem rename_comp (σ : V → R) (τ : R → W) (φ : RSen V ι) :
    φ.rename (τ ∘ σ) = (φ.rename σ).rename τ := by
  cases φ <;> simp [rename, TSen.rename_comp, Set.image_image]

omit [DecidableEq V] [DecidableEq R] in
/-- Agreement on a renamed set of variables is agreement of the reducts. -/
theorem agreeOn_image_iff (σ : V → R) (S : Set V) (s t : Config R ι) :
    Flowgraph.AgreeOn (σ '' S) s t ↔ Flowgraph.AgreeOn S (s.reduct σ) (t.reduct σ) := by
  simp [Flowgraph.AgreeOn, Config.reduct]

/-- The satisfaction condition of the relational institution. -/
theorem holds_rename (σ : V → R) (p : Trans R ι × Trans R ι) (φ : RSen V ι) :
    (φ.rename σ).Holds p ↔ φ.Holds (p.1.reduct σ, p.2.reduct σ) := by
  cases φ with
  | left φ => exact TSen.holds_rename σ p.1 φ
  | right φ => exact TSen.holds_rename σ p.2 φ
  | lockstep n S T =>
    exact imp_congr Iff.rfl (imp_congr (agreeOn_image_iff σ S _ _)
      (and_congr Iff.rfl (agreeOn_image_iff σ (T p.1.dst.pc) _ _)))

end RSen

/-- The relational (2-safety) institution over node labels `ι`. -/
def relLogic (ι : Type) : StateLogic.{0} where
  Sen A := RSen A.V ι
  map σ := RSen.rename (RegSig.ren σ)
  map_id _ := RSen.rename_id
  map_comp σ τ := RSen.rename_comp (RegSig.ren σ) (RegSig.ren τ)
  St A := Trans A.V ι × Trans A.V ι
  reduct σ p := (p.1.reduct (RegSig.ren σ), p.2.reduct (RegSig.ren σ))
  reduct_id _ _ := rfl
  reduct_comp _ _ _ := rfl
  Holds p φ := φ.Holds p
  holds_map σ p φ := RSen.holds_rename (RegSig.ren σ) p φ

/-- The comorphism embedding transition sentences as sentences about the left
transition. -/
def transToRel (ι : Type) : StateLogic.Comorphism (transLogic ι) (relLogic ι) where
  sen φ := .left φ
  st p := p.1
  sen_natural _ _ := rfl
  st_natural _ _ := rfl
  sat _ _ := Iff.rfl

/-- The room of the relational institution at a register file `V`. -/
abbrev relRoom (V ι : Type) [DecidableEq V] : Room.{0, 0, 0} :=
  (relLogic ι).room ⟨V⟩

end Institutions

namespace Flowgraph

open Institutions

variable {V ι : Type} [DecidableEq V]

/-- The product theory: the theory of `G` on the left and of `G'` on the right. -/
def pairAxioms (G G' : Flowgraph V ι) : Set (RSen V ι) :=
  RSen.left '' G.axioms ∪ RSen.right '' G'.axioms

/-- The liveness theory of an assignment `live`: agreement on the live variables
is preserved by a step. -/
def liveAxioms (live : ι → Set V) : Set (RSen V ι) :=
  Set.range fun n => RSen.lockstep n (live n) live

/-- A safe liveness assignment is a sound analysis: its theory is a consequence
of the product theory of `G` with itself (notes p. 8, derived from
`Flowgraph.agree_step`). -/
theorem liveAxioms_entailed (G : Flowgraph V ι) {live : ι → Set V}
    (hlive : G.liveEquations.IsSafe live) :
    ∀ φ ∈ liveAxioms live, (relRoom V ι).Entails (G.pairAxioms G) φ := sorry

/-- Dead-code elimination preserves the liveness theory: it is a consequence of
the product theory of `G` and `G.dceWith live` (slide p. 68, derived from
`Flowgraph.dceWith_agree_step`). -/
theorem dceWith_liveAxioms_entailed (G : Flowgraph V ι) (live : ι → Finset V)
    (hlive : G.liveEquations.IsSafe fun n => (live n : Set V)) :
    ∀ φ ∈ liveAxioms (fun n => (live n : Set V)),
      (relRoom V ι).Entails (G.pairAxioms (G.dceWith live)) φ := sorry

/-- The program `0: x := x; 1: EXIT` over one variable. -/
def selfMove : Flowgraph Unit (Fin 2) where
  entry := 0
  code := ![.alu2 .mov () (.var ()) 1, .exit []]

/-- The converse of `liveAxioms_entailed` fails: for `x := x; EXIT`, the empty
liveness assignment has an entailed theory but is not safe, since `x` is
referenced at node `0`. -/
theorem exists_entailed_not_isSafe :
    ∃ (G : Flowgraph Unit (Fin 2)) (live : Fin 2 → Set Unit),
      (∀ φ ∈ liveAxioms live, (relRoom Unit (Fin 2)).Entails (G.pairAxioms G) φ) ∧
        ¬ G.liveEquations.IsSafe live := sorry

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
