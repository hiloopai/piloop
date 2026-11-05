module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Assertions

/-! # Programs as theories: the transition institution

The 3-address IR as an institution in which a *program is a theory* and the
models of the theory are the *steps of its executions*.

* Signatures are register files `RegSig` (injective renamings); the node labels
  `ι` are a fixed parameter.
* Models over `V` are transitions `Trans V ι`: a source configuration, an
  optional output and a target configuration (a discrete category).
* Sentences `TSen V ι`:
  - `instr n i`: a step from node `n` follows instruction `i` on the control
    flow, the memory, the output and the variables `i` defines (notes p. 4);
  - `frame n x`: a step from node `n` leaves `x` unchanged;
  - `src φ`: the source state satisfies the assertion `φ`;
  - `hoare n φ m ψ`: a Hoare-style assertion: a step from node `n` in a state
    satisfying `φ` that reaches node `m` reaches a state satisfying `ψ`.
* The satisfaction condition (`TSen.holds_rename`) holds along every renaming.

The theory of a flowgraph `G` is presented by `Flowgraph.axioms G`: one `instr`
sentence per node and one `frame` sentence for each variable a node does not
define. Its models are exactly the steps of `G` (`holds_axioms_iff`). Renaming
the theory along an injective renaming gives part of the theory of the renamed
program (`rename_axioms_subset`), so the satisfaction condition alone shows that
reducts of steps of `G.rename σ` are steps of `G` (`step_reduct_of_step_rename`).

A dataflow analysis computing an assertion `A n` at every node is *sound* when
its Hoare sentences `hoare n (A n) m (A m)` are consequences of the program's
theory (`IsInductive`); then every reachable state satisfies the assertion at
its node (`IsInductive.holds_of_steps`), which is the notes' notion of a safe
analysis for forward problems.

`assnToTrans` is the comorphism of institutions from state assertions to the
transition institution: an assertion becomes a statement about the source of a
step.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Logic.Institution

namespace Config

variable {V R ι : Type}

/-- The reduct of a configuration along a renaming. -/
def reduct (σ : V → R) (c : Config R ι) : Config V ι := ⟨c.pc, c.env ∘ σ, c.mem⟩

end Config

namespace Institutions

/-- A transition: a source configuration, an output and a target configuration.
The models of the transition institution. -/
structure Trans (V ι : Type) : Type where
  src : Config V ι
  out : Option ℤ
  dst : Config V ι

namespace Trans

variable {V R ι : Type}

/-- The reduct of a transition along a renaming. -/
def reduct (σ : V → R) (t : Trans R ι) : Trans V ι := ⟨t.src.reduct σ, t.out, t.dst.reduct σ⟩

end Trans

end Institutions

namespace Instr

open Institutions

variable {V ι : Type} [DecidableEq V]

/-- The transition `t` is a step of the instruction `i` on the control flow,
the memory, the output and the variables `i` defines (the frame is separate).
An `EXIT` has no steps. -/
def Realises (i : Instr V ι) (t : Trans V ι) : Prop :=
  match i.exec t.src.env t.src.mem with
  | none => False
  | some (m, env', mem', o) =>
    t.dst.pc = m ∧ t.dst.mem = mem' ∧ t.out = o ∧ ∀ d ∈ i.defs, t.dst.env d = env' d

omit [DecidableEq V] in
theorem rename_id' (i : Instr V ι) : i.rename id = i := sorry

omit [DecidableEq V] in
theorem rename_comp' {R W : Type} (σ : V → R) (τ : R → W) (i : Instr V ι) :
    i.rename (τ ∘ σ) = (i.rename σ).rename τ := sorry

variable {R : Type} [DecidableEq R]

/-- Realisation commutes with renaming (no injectivity needed). -/
theorem realises_rename (σ : V → R) (t : Trans R ι) (i : Instr V ι) :
    (i.rename σ).Realises t ↔ i.Realises (t.reduct σ) := sorry

end Instr

namespace Institutions

/-- The sentences of the transition institution. -/
inductive TSen (V ι : Type) : Type where
  /-- A step from node `n` follows instruction `i`. -/
  | instr (n : ι) (i : Instr V ι)
  /-- A step from node `n` leaves `x` unchanged. -/
  | frame (n : ι) (x : V)
  /-- The source state satisfies `φ`. -/
  | src (φ : Assn V)
  /-- A step from `n` in a `φ`-state that reaches `m` reaches a `ψ`-state. -/
  | hoare (n : ι) (φ : Assn V) (m : ι) (ψ : Assn V)


namespace TSen

variable {V R W ι : Type}

/-- Rename the registers of a sentence. -/
def rename (σ : V → R) : TSen V ι → TSen R ι
  | .instr n i => .instr n (i.rename σ)
  | .frame n x => .frame n (σ x)
  | .src φ => .src (φ.rename σ)
  | .hoare n φ m ψ => .hoare n (φ.rename σ) m (ψ.rename σ)

variable [DecidableEq V] [DecidableEq R]

/-- Satisfaction of a sentence by a transition. -/
def Holds (t : Trans V ι) : TSen V ι → Prop
  | .instr n i => t.src.pc = n → i.Realises t
  | .frame n x => t.src.pc = n → t.dst.env x = t.src.env x
  | .src φ => φ.Holds t.src.env t.src.mem
  | .hoare n φ m ψ => t.src.pc = n → φ.Holds t.src.env t.src.mem → t.dst.pc = m →
      ψ.Holds t.dst.env t.dst.mem

set_option linter.unusedSectionVars false in
theorem rename_id (φ : TSen V ι) : φ.rename id = φ := sorry

set_option linter.unusedSectionVars false in
theorem rename_comp (σ : V → R) (τ : R → W) (φ : TSen V ι) :
    φ.rename (τ ∘ σ) = (φ.rename σ).rename τ := sorry

/-- The satisfaction condition of the transition institution. -/
theorem holds_rename (σ : V → R) (t : Trans R ι) (φ : TSen V ι) :
    (φ.rename σ).Holds t ↔ φ.Holds (t.reduct σ) := sorry

end TSen

/-- The transition institution over node labels `ι`. -/
def transLogic (ι : Type) : StateLogic.{0} where
  Sen A := TSen A.V ι
  map σ := TSen.rename (RegSig.ren σ)
  map_id _ := TSen.rename_id
  map_comp σ τ := TSen.rename_comp (RegSig.ren σ) (RegSig.ren τ)
  St A := Trans A.V ι
  reduct σ := Trans.reduct (RegSig.ren σ)
  reduct_id _ _ := rfl
  reduct_comp _ _ _ := rfl
  Holds t φ := φ.Holds t
  holds_map σ t φ := TSen.holds_rename (RegSig.ren σ) t φ

/-- The comorphism from state assertions to transitions: an assertion holds of
a step when it holds of its source state. -/
def assnToTrans (ι : Type) : StateLogic.Comorphism assnLogic (transLogic ι) where
  sen φ := .src φ
  st t := (t.src.env, t.src.mem)
  sen_natural _ _ := rfl
  st_natural _ _ := rfl
  sat _ _ := Iff.rfl

/-- The room of the transition institution at a register file `V`. -/
abbrev transRoom (V ι : Type) [DecidableEq V] : Room.{0, 0, 0} :=
  (transLogic ι).room ⟨V⟩

end Institutions

namespace Flowgraph

open Institutions

variable {V ι : Type} [DecidableEq V] (G : Flowgraph V ι)

/-- The presentation of the theory of a flowgraph: each node's instruction, and
the frame of every variable it does not define. -/
def axioms : Set (TSen V ι) :=
  {φ | ∃ n, φ = .instr n (G.code n)} ∪ {φ | ∃ n x, x ∉ (G.code n).defs ∧ φ = .frame n x}

/-- Reducts of steps of the renamed program are steps of the original: the
satisfaction condition applied to the theory of `G`. -/
theorem step_reduct_of_step_rename {R : Type} [DecidableEq R] {σ : V → R}
    (hσ : Function.Injective σ) (t : Trans R ι)
    (h : (G.rename σ).step t.src = some (t.out, t.dst)) :
    G.step (t.reduct σ).src = some ((t.reduct σ).out, (t.reduct σ).dst) := sorry

/-- An assignment of assertions to nodes is an inductive invariant of `G`: each
of its Hoare sentences is a consequence of the theory of `G`. -/
def IsInductive (A : ι → Assn V) : Prop :=
  ∀ n m, (transRoom V ι).Entails G.axioms (TSen.hoare n (A n) m (A m))

/-- Soundness of an inductive invariant: every state reachable from a state
satisfying the assertion at its node satisfies the assertion at its node. -/
theorem IsInductive.holds_of_steps {A : ι → Assn V} (hA : G.IsInductive A)
    {s s' : Config V ι} {w : List ℤ} (hs : (A s.pc).Holds s.env s.mem) (h : G.Steps s w s') :
    (A s'.pc).Holds s'.env s'.mem := sorry

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
