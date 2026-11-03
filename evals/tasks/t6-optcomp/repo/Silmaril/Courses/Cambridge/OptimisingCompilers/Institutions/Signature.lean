module

public import Silmaril.Logic.Institution.Morphism
public import Mathlib.CategoryTheory.Discrete.Basic
public import Mathlib.Logic.Embedding.Basic

/-! # Register files as signatures

The common signature category of the institutions of the 3-address IR
(`Institutions.Assertions`, `Institutions.Transitions`, `Institutions.Relational`,
`Institutions.Observational`): a signature is a *register file*, a type `V` of
variables (virtual registers) with decidable equality, and a signature morphism
`V ⟶ V'` is an injective renaming `V ↪ V'`.

Renamings must be injective: along a non-injective renaming, an assignment to
`σ d` also overwrites every other preimage of `σ d`, so the behaviour of a
renamed program is not the behaviour of the original on the reduct state. This
is why register allocation (`Flowgraph.rename_correct`) needs a colouring of the
clash graph rather than an arbitrary map. The node labels `ι` of a flowgraph are
a fixed parameter of the per-node institutions; relabelling nodes is future work.

Models of all four institutions are discrete categories of states. The helper
`discreteReduct` turns a map of states into the reduct functor, and
`discreteReduct_id` and `discreteReduct_comp` discharge the functor laws once.

Source: `cam-optcomp-3addrcode` and `cam-optcomp-2025-handout` (notes p. 4)
in `references.bib`; the
institution framework of `Silmaril.Logic.Institution` (Goguen–Burstall,
Definition 1).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions

open CategoryTheory

/-- A register file: a type of variables with decidable equality. -/
structure RegSig : Type 1 where
  /-- The variables (virtual registers). -/
  V : Type
  [decEq : DecidableEq V]

attribute [instance] RegSig.decEq

/-- A signature morphism: an injective renaming of registers. -/
@[ext]
structure RegSig.Hom (A B : RegSig) : Type where
  /-- The renaming. -/
  emb : A.V ↪ B.V

instance : Category.{0} RegSig where
  Hom := RegSig.Hom
  id A := ⟨Function.Embedding.refl A.V⟩
  comp f g := ⟨f.emb.trans g.emb⟩
  id_comp _ := rfl
  comp_id _ := rfl
  assoc _ _ _ := rfl

namespace RegSig

/-- The underlying function of a renaming. -/
def ren {A B : RegSig} (σ : A ⟶ B) : A.V → B.V := σ.emb

theorem ren_injective {A B : RegSig} (σ : A ⟶ B) : Function.Injective (ren σ) :=
  σ.emb.injective

@[simp] theorem ren_id (A : RegSig) : ren (𝟙 A) = id := sorry

@[simp] theorem ren_comp {A B C : RegSig} (σ : A ⟶ B) (τ : B ⟶ C) :
    ren (σ ≫ τ) = ren τ ∘ ren σ := sorry

end RegSig

/-- The reduct functor of a discrete category of states along a map of states. -/
def discreteReduct {X Y : Type} (f : Y → X) : Discrete Y ⥤ Discrete X :=
  Discrete.functor fun y => Discrete.mk (f y)

@[simp] theorem discreteReduct_obj {X Y : Type} (f : Y → X) (y : Discrete Y) :
    (discreteReduct f).obj y = ⟨f y.as⟩ := sorry

/-- Any two morphisms of a discrete category are equal. -/
theorem discreteHom_eq {α : Type} {X Y : Discrete α} (f g : X ⟶ Y) : f = g :=
  Subsingleton.elim f g

theorem discreteReduct_id (X : Type) : discreteReduct (id : X → X) = 𝟭 (Discrete X) := sorry

theorem discreteReduct_comp {X Y Z : Type} (f : Y → X) (g : Z → Y) :
    discreteReduct (f ∘ g) = discreteReduct g ⋙ discreteReduct f := sorry

theorem discreteReduct_congr {X Y : Type} {f g : Y → X} (h : ∀ y, f y = g y) :
    discreteReduct f = discreteReduct g := sorry

universe w

/-! ## State logics

All four institutions of the 3-address IR have the same shape: sentences that
translate forwards along renamings, a *type* of states (models) that reduct
backwards along renamings, and a satisfaction relation with the satisfaction
condition. `StateLogic` packages this data and `StateLogic.inst` builds the
institution `RegSig ⥤ Room` (Goguen–Burstall, Definition 1, in the room-functor
form of Definition 12), whose model categories are discrete. -/

/-- The data of an institution over register files with discrete model categories. -/
structure StateLogic : Type (max 2 (w + 1)) where
  /-- Sentences over a register file. -/
  Sen : RegSig → Type w
  /-- Sentence translation along a renaming. -/
  map : {A B : RegSig} → (A ⟶ B) → Sen A → Sen B
  map_id : ∀ (A : RegSig) (φ : Sen A), map (𝟙 A) φ = φ
  map_comp : ∀ {A B C : RegSig} (σ : A ⟶ B) (τ : B ⟶ C) (φ : Sen A),
    map (σ ≫ τ) φ = map τ (map σ φ)
  /-- States (models) over a register file. -/
  St : RegSig → Type
  /-- The reduct of a state along a renaming. -/
  reduct : {A B : RegSig} → (A ⟶ B) → St B → St A
  reduct_id : ∀ (A : RegSig) (s : St A), reduct (𝟙 A) s = s
  reduct_comp : ∀ {A B C : RegSig} (σ : A ⟶ B) (τ : B ⟶ C) (s : St C),
    reduct (σ ≫ τ) s = reduct σ (reduct τ s)
  /-- Satisfaction. -/
  Holds : {A : RegSig} → St A → Sen A → Prop
  /-- The satisfaction condition. -/
  holds_map : ∀ {A B : RegSig} (σ : A ⟶ B) (s : St B) (φ : Sen A),
    Holds s (map σ φ) ↔ Holds (reduct σ s) φ

namespace StateLogic

open Silmaril.Logic.Institution

variable (L : StateLogic.{w})

/-- The room of a register file. -/
def room (A : RegSig) : Room.{w, 0, 0} :=
  ⟨L.Sen A, Cat.of (Discrete (L.St A)), fun M φ => L.Holds (Discrete.as M) φ⟩

/-- The corridor of a renaming. -/
def corridor {A B : RegSig} (σ : A ⟶ B) : L.room A ⟶ L.room B :=
  ⟨L.map σ, discreteReduct (L.reduct σ), fun M φ => L.holds_map σ (Discrete.as M) φ⟩

/-- The institution of a state logic. -/
def functor : RegSig ⥤ Room.{w, 0, 0} where
  obj := L.room
  map := L.corridor
  map_id A := Room.hom_ext (funext (L.map_id A))
    (Functor.ext (fun X => congrArg Discrete.mk (L.reduct_id A X.as))
      (fun _ _ _ => discreteHom_eq _ _))
  map_comp σ τ := Room.hom_ext (funext (L.map_comp σ τ))
    (Functor.ext (fun X => congrArg Discrete.mk (L.reduct_comp σ τ X.as))
      (fun _ _ _ => discreteHom_eq _ _))

/-- The bundled institution of a state logic. -/
def inst : Inst.{1, 0, w, 0, 0} := ⟨RegSig, L.functor⟩

/-- A comorphism of state logics over the identity of signatures: sentences
translate forwards, states backwards, naturally in the register file, with the
satisfaction condition `s' ⊨' α φ ↔ β s' ⊨ φ`. -/
structure Comorphism (L L' : StateLogic.{w}) where
  /-- Sentence translation. -/
  sen : {A : RegSig} → L.Sen A → L'.Sen A
  /-- State translation. -/
  st : {A : RegSig} → L'.St A → L.St A
  sen_natural : ∀ {A B : RegSig} (σ : A ⟶ B) (φ : L.Sen A), L'.map σ (sen φ) = sen (L.map σ φ)
  st_natural : ∀ {A B : RegSig} (σ : A ⟶ B) (s : L'.St B), st (L'.reduct σ s) = L.reduct σ (st s)
  /-- The satisfaction condition of the comorphism. -/
  sat : ∀ {A : RegSig} (s : L'.St A) (φ : L.Sen A), L'.Holds s (sen φ) ↔ L.Holds (st s) φ

/-- The institution comorphism of a comorphism of state logics (Goguen–Roşu,
Definition 4.2), with the identity signature functor. -/
def Comorphism.coHom {L L' : StateLogic.{w}} (c : Comorphism L L') : Inst.CoHom L.inst L'.inst where
  Φ := 𝟭 RegSig
  ρ :=
    { app _ := ⟨c.sen, discreteReduct c.st, fun M φ => c.sat (Discrete.as M) φ⟩
      naturality _ _ σ := Room.hom_ext (funext fun φ => (c.sen_natural σ φ).symm)
        (Functor.ext (fun X => congrArg Discrete.mk (c.st_natural σ X.as).symm)
          (fun _ _ _ => discreteHom_eq _ _)) }

end StateLogic

end Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions
