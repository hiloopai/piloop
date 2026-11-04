module

public import Silmaril.Logic.Institution.Room
public import Mathlib.CategoryTheory.Opposites

@[expose] public section

/-!
# Institutions

An institution over a category of signatures `Sign` has a sentence functor `Sen : Sign ⥤ Type`,
a model functor `Mod : Signᵒᵖ ⥤ Cat` and, for each signature, a satisfaction relation, such that
truth is invariant under change of notation (the satisfaction condition):

  `M' ⊨_{Σ'} Sen(σ) φ ↔ Mod(σ) M' ⊨_Σ φ`   for `σ : Σ ⟶ Σ'`.

This is `ClassicInstitution Sign` (Goguen–Burstall, Definition 1; Goguen–Roşu call it the "classic" notion). Equivalently — and this is the form
used in the rest of `Silmaril.Logic.Institution` — an institution is a functor `Sign ⥤ Room`:
each signature gives a room `(Sen Σ, Mod Σ, ⊨_Σ)` and each signature morphism a corridor, whose
satisfaction condition is the satisfaction condition of the institution (Goguen–Burstall,
Definition 12; Mossakowski–Diaconescu–Tarlecki, Definition 4.2). `ClassicInstitution.equivRoomFunctor`
is the bijection between the two presentations (Goguen–Roşu, Proposition 3.8).

All notions are universe polymorphic: signatures live in `Type u` with morphisms in `Type v`,
sentences in `Type w`, and models form a `Cat.{m₁, m₂}`.

## Main definitions

* `ClassicInstitution Sign`: the direct definition.
* `ClassicInstitution.toRoomFunctor`, `ClassicInstitution.ofRoomFunctor`, `ClassicInstitution.equivRoomFunctor`.
* `sentenceFunctor I`, `modelFunctor I` for a room functor `I : Sign ⥤ Room`.
-/

namespace Silmaril.Logic.Institution

open CategoryTheory Opposite

universe u v w m₁ m₂

/-- An institution over `Sign`: sentences, models, satisfaction, and the satisfaction condition.

Source: `goguen-burstall-1992` in `references.bib`, §2.1, Definition 1;
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §3, Definition 3.1 (the
"classic" notion). -/
structure ClassicInstitution (Sign : Type u) [Category.{v} Sign] where
  /-- The sentence functor `Sen : Sign ⥤ Set`. -/
  Sen : Sign ⥤ Type w
  /-- The model functor `Mod : Signᵒᵖ ⥤ Cat`. -/
  Mod : Signᵒᵖ ⥤ Cat.{m₁, m₂}
  /-- Satisfaction `M ⊨_Σ φ`. -/
  Sat : (S : Sign) → Mod.obj (op S) → Sen.obj S → Prop
  /-- The satisfaction condition: truth is invariant under change of notation. -/
  satisfaction : ∀ {S S' : Sign} (σ : S ⟶ S') (M' : Mod.obj (op S')) (φ : Sen.obj S),
    Sat S' M' (Sen.map σ φ) ↔ Sat S ((Mod.map σ.op).toFunctor.obj M') φ

namespace ClassicInstitution

variable {Sign : Type u} [Category.{v} Sign]

/-- The room of a signature: `(Sen Σ, Mod Σ, ⊨_Σ)`.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4,
Definition 4.2. -/
def room (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) (S : Sign) : Room.{w, m₁, m₂} :=
  ⟨I.Sen.obj S, I.Mod.obj (op S), I.Sat S⟩

/-- The corridor of a signature morphism `σ : Σ ⟶ Σ'`: `(Sen σ, Mod σ)`.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4,
Definition 4.2. -/
def corridor (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) {S S' : Sign} (σ : S ⟶ S') :
    I.room S ⟶ I.room S' :=
  ⟨I.Sen.map σ, (I.Mod.map σ.op).toFunctor, I.satisfaction σ⟩

theorem corridor_id (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) (S : Sign) :
    I.corridor (𝟙 S) = 𝟙 (I.room S) := by
  apply Room.hom_ext
  · funext φ
    exact I.Sen.map_id_apply S φ
  · change (I.Mod.map (𝟙 S).op).toFunctor = 𝟭 _
    rw [op_id, Functor.map_id]
    rfl

theorem corridor_comp (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) {S S' S'' : Sign} (σ : S ⟶ S')
    (τ : S' ⟶ S'') : I.corridor (σ ≫ τ) = I.corridor σ ≫ I.corridor τ := by
  apply Room.hom_ext
  · funext φ
    exact I.Sen.map_comp_apply σ τ φ
  · change (I.Mod.map (σ ≫ τ).op).toFunctor =
      (I.Mod.map τ.op).toFunctor ⋙ (I.Mod.map σ.op).toFunctor
    rw [op_comp, Functor.map_comp]
    rfl

/-- An institution as a functor into rooms.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 12 (with
the category-of-models variant of `Trel`);
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4,
Definition 4.2. -/
def toRoomFunctor (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) : Sign ⥤ Room.{w, m₁, m₂} where
  obj := I.room
  map := I.corridor
  map_id := I.corridor_id
  map_comp := I.corridor_comp

end ClassicInstitution

/-! ### From a room functor to an institution -/

section RoomFunctor

variable {Sign : Type u} [Category.{v} Sign] (I : Sign ⥤ Room.{w, m₁, m₂})

/-- The sentence functor of a room functor, `I ⋙ Right`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 12 ("the
composition 𝒥; Right … is the sentence functor"). -/
def sentenceFunctor : Sign ⥤ Type w := I ⋙ Room.senFunctor

/-- The model functor of a room functor, `(I ⋙ Left)` turned into a functor on `Signᵒᵖ`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 12 ("the
composition 𝒥; Left … is the model functor"). -/
def modelFunctor : Signᵒᵖ ⥤ Cat.{m₁, m₂} := (I ⋙ Room.modFunctor).leftOp

/-- The satisfaction condition, read off a room functor.

Source: `goguen-burstall-1992` in `references.bib`, §2.1, Definition 1;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4,
Definition 4.2. -/
theorem satisfaction_condition {S S' : Sign} (σ : S ⟶ S') (M' : (I.obj S').Mod)
    (φ : (I.obj S).Sen) :
    (I.obj S').Sat M' ((I.map σ).sen φ) ↔ (I.obj S).Sat ((I.map σ).mod.obj M') φ :=
  (I.map σ).sat M' φ

/-- The institution in the sense of Definition 1 determined by a room functor.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Proposition 3.8
(the institution associated with a functor `Sign → Trel`). -/
def ClassicInstitution.ofRoomFunctor : ClassicInstitution.{u, v, w, m₁, m₂} Sign where
  Sen := sentenceFunctor I
  Mod := modelFunctor I
  Sat S := (I.obj S).Sat
  satisfaction σ M' φ := (I.map σ).sat M' φ

end RoomFunctor

namespace ClassicInstitution

variable {Sign : Type u} [Category.{v} Sign]

/-- Round trip from the direct definition.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Proposition 3.8
("these two associations are inverse to each other"). -/
theorem ofRoomFunctor_toRoomFunctor (I : ClassicInstitution.{u, v, w, m₁, m₂} Sign) :
    ofRoomFunctor I.toRoomFunctor = I := by
  cases I
  rfl

/-- Round trip from the room-functor definition.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.8. -/
theorem toRoomFunctor_ofRoomFunctor (I : Sign ⥤ Room.{w, m₁, m₂}) :
    (ofRoomFunctor I).toRoomFunctor = I :=
  rfl

/-- Institutions over `Sign` are the same as functors `Sign ⥤ Room`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Proposition 3.8;
`goguen-burstall-1992` in `references.bib`, §2.5, Definition 12. -/
def equivRoomFunctor : ClassicInstitution.{u, v, w, m₁, m₂} Sign ≃ (Sign ⥤ Room.{w, m₁, m₂}) where
  toFun I := I.toRoomFunctor
  invFun I := ofRoomFunctor I
  left_inv := ofRoomFunctor_toRoomFunctor
  right_inv := toRoomFunctor_ofRoomFunctor

end ClassicInstitution

end Silmaril.Logic.Institution
