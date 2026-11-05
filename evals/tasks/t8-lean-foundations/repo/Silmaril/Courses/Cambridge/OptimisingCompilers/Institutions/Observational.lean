module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Signature
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Assertions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.CommonSubexpression
public import Silmaril.Courses.Cambridge.OptimisingCompilers.DeadCode
public import Silmaril.Logic.Institution.Observational.Sentence
public import Mathlib.Data.Fintype.Sum

/-! # Transformations as corridors: the observational institution

The 3-address IR as an *observational* institution, using the shared
`Silmaril.Logic.Institution.Observational.Sentence` syntax:

* Signatures: register files `RegSig` (injective renamings).
* Sentences over `V`: observational equations `P ≐ Q` and refinements `P ⊑ Q`
  between *programs* `Prog V` (finite flowgraphs over `V`, with any node type).
* Models over `V`: initial states `(env, mem)` (a discrete category).
* `(env, mem) ⊨ P ≐ Q` iff `P` and `Q`, started at their entries in
  `(env, mem)`, have the same output traces and the same halting outcomes
  (`Prog.beh`); `⊑` is inclusion.
* The satisfaction condition along an injective renaming `σ` is
  `Prog.beh_rename`: the renamed program run from `env'` behaves like the
  original run from the reduct `env' ∘ σ`. It is derived from the course's
  register-allocation theorem `Flowgraph.rename_correct` (an injective renaming
  colours every clash graph).

**Transformations.** A program transformation `T : Prog V → Prog V` is *sound*
(`Transformation.Sound`) when every model satisfies `T P ≐ P`: the transformation
is valid in the observational institution. A sound transformation acts on
sentences by `Observational.Sentence.map T` and on models by the identity, and
this pair is a corridor of the observational room (`Transformation.corridor`):
its satisfaction condition is the correctness of the transformation. A family of
sound transformations natural in renamings is an institution comorphism of the
observational institution into itself (`SoundFamily.coHom`).

**Instances.**

* Dead-code elimination (slide p. 68): `dce` is sound (`dce_sound`, from the
  course's `Flowgraph.dce_obsEquiv`). It commutes with injective renamings
  (`dce_rename`), so it is a comorphism (`dceFamily`).
* Common-subexpression elimination and copy propagation (notes §5,
  pp. 13–14) are sound *under side conditions* on the program; they give
  sentences valid in every model (`cse_valid`, `copyProp_valid`), from the
  course's `cse_correct` and `copyProp_correct`.
* Register allocation (notes §4, pp. 10–13) renames along a *non-injective*
  colouring, which is not a signature morphism; its correctness is the
  satisfaction condition restricted to programs whose clash graph the renaming
  colours (`beh_rename_of_isColouring`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Logic.Institution CategoryTheory

namespace Institutions

/-! ## Renaming lemmas for liveness and DCE

Helpers for `dce_rename`: along an injective renaming, the LVA equations of the
renamed program are the image of the original ones, so the least solution (and
hence the LVA algorithm's result) of the renamed program is the image of the
original's. -/

section RenameLemmas

variable {V R ι : Type}

theorem operand_rename_id (a : Operand V) : a.rename id = a := sorry

theorem operand_rename_comp {W : Type} (σ : V → R) (τ : R → W) (a : Operand V) :
    a.rename (τ ∘ σ) = (a.rename σ).rename τ := sorry

theorem instr_rename_id (i : Instr V ι) : i.rename id = i := sorry

theorem instr_rename_comp {W : Type} (σ : V → R) (τ : R → W) (i : Instr V ι) :
    i.rename (τ ∘ σ) = (i.rename σ).rename τ := sorry

theorem instr_succs_rename (σ : V → R) (i : Instr V ι) : (i.rename σ).succs = i.succs := sorry

variable [DecidableEq V] [DecidableEq R]

omit [DecidableEq V] in
theorem operand_vars_rename (σ : V → R) (a : Operand V) :
    (a.rename σ).vars = a.vars.image σ := sorry

omit [DecidableEq V] in
theorem instr_defs_rename (σ : V → R) (i : Instr V ι) :
    (i.rename σ).defs = i.defs.image σ := sorry

theorem instr_refs_rename (σ : V → R) (i : Instr V ι) :
    (i.rename σ).refs = i.refs.image σ := sorry

omit [DecidableEq V] [DecidableEq R] in
theorem flowgraph_rename_code (G : Flowgraph V ι) (σ : V → R) (n : ι) :
    (G.rename σ).code n = (G.code n).rename σ := sorry

omit [DecidableEq V] [DecidableEq R] in
theorem flowgraph_succ_rename (G : Flowgraph V ι) (σ : V → R) :
    (G.rename σ).succ = G.succ := sorry

/-- One round of the renamed LVA equations on an image is the image of one round. -/
theorem liveStep_image (G : Flowgraph V ι) {σ : V → R} (hσ : Function.Injective σ)
    (x : ι → Set V) :
    (G.rename σ).liveEquations.step (fun n => σ '' x n) =
      fun n => σ '' G.liveEquations.step x n := sorry

/-- Along an injective renaming, liveness of the renamed program is the image of
liveness. -/
theorem live_rename (G : Flowgraph V ι) {σ : V → R} (hσ : Function.Injective σ) :
    (G.rename σ).live = fun n => σ '' G.live n := sorry

theorem lva_rename [Fintype ι] [DecidableEq ι] (G : Flowgraph V ι) {σ : V → R}
    (hσ : Function.Injective σ) (n : ι) :
    (G.rename σ).lva n = (G.lva n).image σ := sorry

/-- DCE commutes with injective renamings, on flowgraphs. -/
theorem flowgraph_dce_rename [Fintype ι] [DecidableEq ι] (G : Flowgraph V ι) {σ : V → R}
    (hσ : Function.Injective σ) : G.dce.rename σ = (G.rename σ).dce := sorry

end RenameLemmas

/-- Observations of a run from the entry: an output trace, or a halting outcome. -/
abbrev Obs : Type := List ℤ ⊕ Flowgraph.Outcome

/-- A program over registers `V`: a finite flowgraph with any type of nodes. -/
structure Prog (V : Type) : Type 1 where
  /-- The nodes. -/
  ι : Type
  [fintype : Fintype ι]
  [decEq : DecidableEq ι]
  /-- The flowgraph. -/
  G : Flowgraph V ι

attribute [instance] Prog.fintype Prog.decEq

namespace Prog

variable {V R W : Type}

/-- The behaviour of a program started at its entry in state `s`: its output
traces and its halting outcomes. -/
def beh [DecidableEq V] (P : Prog V) (s : State V) : Set Obs :=
  Sum.inl '' P.G.traces ⟨P.G.entry, s.1, s.2⟩ ∪ Sum.inr '' P.G.outcomes ⟨P.G.entry, s.1, s.2⟩

/-- Rename the registers of a program. -/
def rename (σ : V → R) (P : Prog V) : Prog R := ⟨P.ι, P.G.rename σ⟩

theorem rename_id (P : Prog V) : P.rename id = P := sorry

theorem rename_comp (σ : V → R) (τ : R → W) (P : Prog V) :
    P.rename (τ ∘ σ) = (P.rename σ).rename τ := sorry

/-- Equal behaviours are equal traces and equal outcomes. -/
theorem beh_eq_iff [DecidableEq V] (P Q : Prog V) (s : State V) :
    P.beh s = Q.beh s ↔
      P.G.traces ⟨P.G.entry, s.1, s.2⟩ = Q.G.traces ⟨Q.G.entry, s.1, s.2⟩ ∧
        P.G.outcomes ⟨P.G.entry, s.1, s.2⟩ = Q.G.outcomes ⟨Q.G.entry, s.1, s.2⟩ := sorry

/-- Register allocation as a satisfaction condition: renaming along a colouring
of the clash graph preserves the behaviour from the reduct state (derived from
`Flowgraph.rename_correct`). -/
theorem beh_rename_of_isColouring [DecidableEq V] [DecidableEq R] (P : Prog V) {σ : V → R}
    (hσ : P.G.IsColouring σ) (s : State R) :
    (P.rename σ).beh s = P.beh (State.reduct σ s) := sorry

/-- The satisfaction condition of the observational institution: an injective
renaming colours every clash graph. -/
theorem beh_rename [DecidableEq V] [DecidableEq R] (P : Prog V) {σ : V → R}
    (hσ : Function.Injective σ) (s : State R) :
    (P.rename σ).beh s = P.beh (State.reduct σ s) := sorry

end Prog

/-- The observational institution of the 3-address IR. -/
def obsLogic : StateLogic.{1} where
  Sen A := Observational.Sentence (Prog A.V)
  map σ := Observational.Sentence.map (Prog.rename (RegSig.ren σ))
  map_id _ φ := by
    cases φ <;> simp [Observational.Sentence.map, Prog.rename_id]
  map_comp σ τ φ := by
    cases φ <;> simp [Observational.Sentence.map, Prog.rename_comp]
  St A := State A.V
  reduct σ := State.reduct (RegSig.ren σ)
  reduct_id _ _ := rfl
  reduct_comp _ _ _ := rfl
  Holds s φ := φ.Holds fun P => P.beh s
  holds_map σ s φ := by
    cases φ <;> simp only [Observational.Sentence.map, Observational.Sentence.Holds,
      Prog.beh_rename _ (RegSig.ren_injective σ)]

theorem obsLogic_holds {A : RegSig} (s : State A.V) (φ : Observational.Sentence (Prog A.V)) :
    obsLogic.Holds s φ ↔ φ.Holds fun P => P.beh s := sorry

theorem obsLogic_map {A B : RegSig} (σ : A ⟶ B) (φ : Observational.Sentence (Prog A.V)) :
    obsLogic.map σ φ = Observational.Sentence.map (Prog.rename (RegSig.ren σ)) φ := sorry

/-- The observational room at a register file `V`. -/
abbrev obsRoom (V : Type) [DecidableEq V] : Room.{1, 0, 0} := obsLogic.room ⟨V⟩

/-- `s ⊨ P ≐ Q` is the course's observational equivalence of the two programs
from their entries. -/
theorem holds_eq_iff {V : Type} [DecidableEq V] (P Q : Prog V) (s : State V) :
    obsLogic.Holds (A := ⟨V⟩) s (Observational.Sentence.eq P Q) ↔
      P.G.traces ⟨P.G.entry, s.1, s.2⟩ = Q.G.traces ⟨Q.G.entry, s.1, s.2⟩ ∧
        P.G.outcomes ⟨P.G.entry, s.1, s.2⟩ = Q.G.outcomes ⟨Q.G.entry, s.1, s.2⟩ := sorry

/-! ## Transformations -/

/-- A program transformation over registers `V`. -/
abbrev Transformation (V : Type) : Type 1 := Prog V → Prog V

namespace Transformation

variable {V : Type} [DecidableEq V]

/-- A transformation is sound when `T P ≐ P` is valid: it holds in every model. -/
def Sound (T : Transformation V) : Prop :=
  ∀ (P : Prog V) (s : State V),
    obsLogic.Holds (A := ⟨V⟩) s (Observational.Sentence.eq (T P) P)

/-- Soundness is validity of the equations `T P ≐ P`, i.e. their consequence from
the empty theory. -/
theorem sound_iff_entails (T : Transformation V) :
    T.Sound ↔ ∀ P, (obsRoom V).Entails ∅ (Observational.Sentence.eq (T P) P) := sorry

/-- A sound transformation preserves behaviours. -/
theorem Sound.beh_eq {T : Transformation V} (hT : T.Sound) (P : Prog V) (s : State V) :
    (T P).beh s = P.beh s := sorry

/-- A sound transformation preserves satisfaction of every sentence. -/
theorem Sound.holds_map_iff {T : Transformation V} (hT : T.Sound) (s : State V)
    (φ : Observational.Sentence (Prog V)) :
    obsLogic.Holds (A := ⟨V⟩) s (Observational.Sentence.map T φ) ↔
      obsLogic.Holds (A := ⟨V⟩) s φ := sorry

/-- Sound transformations compose. -/
theorem Sound.comp {T T' : Transformation V} (hT : T.Sound) (hT' : T'.Sound) :
    Transformation.Sound (T' ∘ T) := sorry

/-- A sound transformation is a corridor of the observational room: sentences
are transformed, models are unchanged, and the satisfaction condition
`s ⊨ T(φ) ↔ s ⊨ φ` is its correctness. -/
def corridor (T : Transformation V) (hT : T.Sound) : obsRoom V ⟶ obsRoom V where
  sen := Observational.Sentence.map T
  mod := 𝟭 _
  sat M φ := hT.holds_map_iff M.as φ

end Transformation

/-- A family of transformations, one for every register file, each sound and
all commuting with injective renamings. -/
structure SoundFamily where
  /-- The transformation at each register file. -/
  T : (A : RegSig) → Transformation A.V
  sound : ∀ A, (T A).Sound
  natural : ∀ {A B : RegSig} (σ : A ⟶ B) (P : Prog A.V),
    (T A P).rename (RegSig.ren σ) = T B (P.rename (RegSig.ren σ))

/-- A sound natural family of transformations is a comorphism of the
observational institution into itself, with identity signature functor and
identity model reducts. -/
def SoundFamily.comorphism (F : SoundFamily) : StateLogic.Comorphism obsLogic obsLogic where
  sen φ := Observational.Sentence.map (F.T _) φ
  st s := s
  sen_natural σ φ := by
    cases φ with
    | eq a b => exact congrArg₂ Observational.Sentence.eq (F.natural σ a) (F.natural σ b)
    | le a b => exact congrArg₂ Observational.Sentence.le (F.natural σ a) (F.natural σ b)
  st_natural _ _ := rfl
  sat s φ := (F.sound _).holds_map_iff s φ

/-- The institution comorphism of a sound natural family. -/
def SoundFamily.coHom (F : SoundFamily) : Inst.CoHom obsLogic.inst obsLogic.inst :=
  F.comorphism.coHom

/-! ## Dead-code elimination -/

/-- Dead-code elimination driven by the LVA algorithm, on programs. -/
def dce {V : Type} [DecidableEq V] : Transformation V := fun P => ⟨P.ι, P.G.dce⟩

/-- DCE is sound (slide p. 68: "the program resulting from this transformation
will remain correct"), from `Flowgraph.dce_obsEquiv`. -/
theorem dce_sound {V : Type} [DecidableEq V] : (dce : Transformation V).Sound := sorry

/-- DCE commutes with injective renamings: the LVA solution of the renamed
program is the image of the original's. -/
theorem dce_rename {V R : Type} [DecidableEq V] [DecidableEq R] {σ : V → R}
    (hσ : Function.Injective σ) (P : Prog V) :
    (dce P).rename σ = dce (P.rename σ) := sorry

/-- DCE as a sound natural family. -/
def dceFamily : SoundFamily where
  T _ := dce
  sound _ := dce_sound
  natural σ P := dce_rename (RegSig.ren_injective σ) P

/-! ## Conditional transformations: CSE and copy propagation -/

/-- CSE (notes §5, p. 13) yields a valid observational equation for programs
satisfying its side conditions (derived from `Flowgraph.cse_correct`). -/
theorem cse_valid {V ι : Type} [DecidableEq V] [Fintype ι] [DecidableEq ι] (G : Flowgraph V ι)
    {e : Rhs V} {t : V} {targets : ι → Bool} (ht : G.Fresh t)
    (htargets : ∀ n, targets n = true → e ∈ G.avail n ∧ ∃ x, (G.code n).assignment? = some (x, e))
    (hentry : G.pred G.entry = ∅) (s : State V) :
    obsLogic.Holds (A := ⟨V⟩) s
      (Observational.Sentence.eq (P := Prog V) ⟨ι ⊕ ι, G.cse e t targets⟩ ⟨ι, G⟩) := sorry

/-- Copy propagation (notes §5, p. 14) yields a valid observational equation for
programs satisfying its side conditions (derived from
`Flowgraph.copyProp_correct`). -/
theorem copyProp_valid {V ι : Type} [DecidableEq V] [Fintype ι] [DecidableEq ι]
    (G : Flowgraph V ι) {x y : V} {sites : ι → Bool}
    (hsites : ∀ n, sites n = true → (x, y) ∈ G.copies n) (hentry : G.pred G.entry = ∅)
    (s : State V) :
    obsLogic.Holds (A := ⟨V⟩) s
      (Observational.Sentence.eq (P := Prog V) ⟨ι, G.copyProp x y sites⟩ ⟨ι, G⟩) := sorry

end Institutions

end Silmaril.Courses.Cambridge.OptimisingCompilers
