module

public import Silmaril.Logic.Institution.Basic
public import Mathlib.CategoryTheory.ObjectProperty.FullSubcategory
public import Mathlib.CategoryTheory.Adjunction.Basic
public import Mathlib.CategoryTheory.Adjunction.Reflective

@[expose] public section

/-!
# Presentations, specifications, theories and their models

For an institution given as a room functor `I : Sign ⥤ Room`:

* a *presentation* (specification) `(Σ, E)` is a signature with a set of `Σ`-sentences. There
  are two notions of morphism `(Σ, E) → (Σ', E')`, both signature morphisms `σ`:
  - the *syntactic* (stricter) presentation morphisms, `σ(E) ⊆ E'`, forming
    `Presentation I`. Neither Goguen–Burstall (who define only theory morphisms, Definition 5)
    nor Goguen–Roşu use this notion; it is kept because `Theory I` is defined as a full
    subcategory of it and `Presentation.empty` is used for theoroidal comorphisms;
  - Goguen–Roşu's *specification morphisms*, `σ(E) ⊆ E'•` (Definition 3.4), forming `Spec I`;
* a *theory* is a presentation whose sentences are closed, `E = E•`; theories form the full
  subcategory `Theory I` (Goguen–Burstall's `Th`), with forgetful functor `Theory.forget I` to
  `Sign`. On theories both notions of morphism agree;
* `Theory.ofPresentation` sends `(Σ, E)` to the theory `(Σ, E•)` it presents, and is left
  adjoint to the inclusion `Th ⊆ Presentation` (`Theory.closureAdjunction`);
* with Goguen–Roşu's semantic morphisms, `(Σ, E) ≅ (Σ, E•)` in `Spec` via identities, so the
  inclusion `Theory.toSpec : Th ⥤ Spec` is an equivalence (`Spec.equivalence`,
  `Spec.toSpec_isEquivalence`) and closure `Spec.closure` is both its left adjoint
  (`Spec.closureAdjunction`) and its right adjoint (`Spec.inclusionAdjunction`): `Th` is
  reflective and coreflective in `Spec`, exactly as stated in Goguen–Roşu 2002, §3.1, the
  paragraph after Definition 3.4;
* the models of a theory form the full subcategory `Theory.Models T` of `Mod Σ`, and a theory
  morphism `σ : T ⟶ T'` has a reduct functor `T'.Models ⥤ T.Models` (Goguen–Burstall,
  Definition 6);
* theories with their model categories again form an institution, `Theory.institution I`, the
  institution `Iᵗʰ` used for theoroidal comorphisms.

For the syntactic `Presentation I` only the reflection holds: in an institution where `∅• ≠ ∅`
(for instance propositional logic, where `⊥ → ⊥` is valid), the presentation `(Σ, ∅)` is not
isomorphic in `Presentation I` to any theory. This is not a counterexample to Goguen–Roşu, whose
Definition 3.4 requires only `φ(F) ⊆ F'•` (an earlier version of this file misread it as
`φ(F) ⊆ F'`, the closure bullet having been lost in text extraction).
-/

namespace Silmaril.Logic.Institution

open CategoryTheory

universe u v w m₁ m₂

variable {Sign : Type u} [Category.{v} Sign] (I : Sign ⥤ Room.{w, m₁, m₂})

/-- A presentation (specification) `(Σ, E)`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(1);
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Definition 3.4. -/
structure Presentation where
  /-- The signature `Σ`. -/
  sig : Sign
  /-- The axioms `E ⊆ Sen Σ`. -/
  axioms : Set (I.obj sig).Sen

namespace Presentation

variable {I}

/-- A syntactic presentation morphism: a signature morphism mapping axioms to axioms,
`σ(E) ⊆ E'`. This is stricter than Goguen–Roşu's specification morphisms (`Spec.Hom`,
`σ(E) ⊆ E'•`); the two agree between theories.

Source: compare `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Definition 3.4 (p. 8) and `goguen-burstall-1992` in `references.bib`, §2.2,
Definition 5 (theory morphisms). -/
@[ext]
structure Hom (P Q : Presentation I) where
  /-- The underlying signature morphism. -/
  sig : P.sig ⟶ Q.sig
  /-- `σ(E) ⊆ E'`. -/
  mapsTo : Set.MapsTo (I.map sig).sen P.axioms Q.axioms

theorem mapsTo_id (P : Presentation I) : Set.MapsTo (I.map (𝟙 P.sig)).sen P.axioms P.axioms := by
  (intro φ hφ; rw [I.map_id]; exact hφ)

theorem Hom.mapsTo_comp {P Q R : Presentation I} (f : Hom P Q) (g : Hom Q R) :
    Set.MapsTo (I.map (f.sig ≫ g.sig)).sen P.axioms R.axioms := by
  (intro φ hφ; rw [I.map_comp]; exact g.mapsTo (f.mapsTo hφ))

/-- The category of presentations with syntactic morphisms. Goguen–Roşu's `Spec` is `Spec I`,
which has the same objects but more morphisms. -/
instance category : Category.{v} (Presentation I) where
  Hom := Hom
  id P := ⟨𝟙 P.sig, mapsTo_id P⟩
  comp f g := ⟨f.sig ≫ g.sig, f.mapsTo_comp g⟩
  id_comp f := Hom.ext (Category.id_comp f.sig)
  comp_id f := Hom.ext (Category.comp_id f.sig)
  assoc f g h := Hom.ext (Category.assoc f.sig g.sig h.sig)

@[ext]
theorem hom_ext {P Q : Presentation I} {f g : P ⟶ Q} (h : f.sig = g.sig) : f = g := Hom.ext h

variable (I) in
/-- The forgetful functor `Presentation I ⥤ Sign`. -/
def forget : Presentation I ⥤ Sign where
  obj P := P.sig
  map f := f.sig

/-- The closure lemma lifted to presentation morphisms: `σ(E•) ⊆ E'•`.

Source: `goguen-burstall-1992` in `references.bib`, §2.3, Lemma 7 and Lemma 8. -/
theorem Hom.mapsTo_closure {P Q : Presentation I} (f : P ⟶ Q) :
    Set.MapsTo (I.map f.sig).sen ((I.obj P.sig).closure P.axioms)
      ((I.obj Q.sig).closure Q.axioms) := by
  intro φ hφ M' hM'
  exact ((I.map f.sig).sat M' φ).2
    (hφ _ fun ψ hψ => ((I.map f.sig).sat M' ψ).1 (hM' _ (f.mapsTo hψ)))

end Presentation

variable {I} in
/-- The presentations whose axioms are closed.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(7). -/
def Presentation.IsTheory : ObjectProperty (Presentation I) :=
  fun P => (I.obj P.sig).IsClosed P.axioms

/-- The category `Th(I)` of theories: the full subcategory of presentations with closed axioms.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definitions 2(7)
and 5; `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Definition 3.4. -/
abbrev Theory : Type (max u w) := (Presentation.IsTheory (I := I)).FullSubcategory

namespace Theory

variable {I}

/-- The signature of a theory. -/
abbrev sig (T : Theory I) : Sign := T.obj.sig

/-- The (closed) set of sentences of a theory. -/
abbrev sentences (T : Theory I) : Set (I.obj T.sig).Sen := T.obj.axioms

theorem isClosed (T : Theory I) : (I.obj T.sig).IsClosed T.sentences := T.property

/-- Theory morphisms are determined by their signature morphisms.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 5. -/
theorem hom_ext {T T' : Theory I} {f g : T ⟶ T'} (h : f.hom.sig = g.hom.sig) : f = g := by
  exact ObjectProperty.hom_ext _ (Presentation.Hom.ext h)

/-- A signature morphism with `σ(T) ⊆ T'` is a theory morphism.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 5. -/
def homMk {T T' : Theory I} (σ : T.sig ⟶ T'.sig)
    (h : Set.MapsTo (I.map σ).sen T.sentences T'.sentences) : T ⟶ T' :=
  ObjectProperty.homMk ⟨σ, h⟩

/-- The presentation lemma: for presentations `(Σ, E)` and `(Σ', E')`, a signature morphism `σ`
is a theory morphism `(Σ, E•) → (Σ', E'•)` iff `σ(E) ⊆ E'•`.

Source: `goguen-burstall-1992` in `references.bib`, §2.3, Lemma 8. -/
theorem presentation_lemma {S S' : Sign} (σ : S ⟶ S') (E : Set (I.obj S).Sen)
    (E' : Set (I.obj S').Sen) :
    Set.MapsTo (I.map σ).sen ((I.obj S).closure E) ((I.obj S').closure E') ↔
      Set.MapsTo (I.map σ).sen E ((I.obj S').closure E') := by
  constructor
  · intro h φ hφ
    apply h
    intro M hM
    exact hM φ hφ
  · intro h φ hφ M' hM'
    exact ((I.map σ).sat M' φ).2
      (hφ _ fun ψ hψ => ((I.map σ).sat M' ψ).1 (h hψ M' hM'))

variable (I) in
/-- The forgetful functor `Th ⥤ Sign`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, the paragraph after
Definition 2 ("a forgetful functor Sign: Th → Sign"). -/
def forget : Theory I ⥤ Sign := ObjectProperty.ι _ ⋙ Presentation.forget I

variable (I) in
/-- The theory presented by a presentation, `(Σ, E) ↦ (Σ, E•)`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(8);
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, after Definition 3.4. -/
def ofPresentation : Presentation I ⥤ Theory I where
  obj P := ⟨⟨P.sig, (I.obj P.sig).closure P.axioms⟩, (I.obj P.sig).closure_isClosed P.axioms⟩
  map f := ObjectProperty.homMk ⟨f.sig, f.mapsTo_closure⟩
  map_id _ := rfl
  map_comp _ _ := rfl

/-- `Th` is a reflective subcategory of the syntactic `Presentation I`: closure is left adjoint
to the inclusion. (It is not coreflective there; for Goguen–Roşu's `Spec`, where it is both, see
`Spec.closureAdjunction` and `Spec.inclusionAdjunction`.)

Source: `goguen-burstall-1992` in `references.bib`, §2.3, Lemma 8; compare
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, the paragraph after
Definition 3.4 (p. 8). -/
def closureAdjunction : ofPresentation I ⊣ ObjectProperty.ι _ :=
  Adjunction.mkOfHomEquiv
    { homEquiv P T :=
        { toFun f := ⟨f.hom.sig, fun _ hφ => f.hom.mapsTo
            ((I.obj P.sig).subset_closure P.axioms hφ)⟩
          invFun g := ObjectProperty.homMk ⟨g.sig, fun φ hφ =>
            Eq.mp (congrArg (fun E => (I.map g.sig).sen φ ∈ E) T.isClosed)
              ((presentation_lemma g.sig P.axioms T.sentences).2
                (fun _ hψ => (I.obj T.sig).subset_closure _ (g.mapsTo hψ)) hφ)⟩
          left_inv _ := rfl
          right_inv _ := rfl }
      homEquiv_naturality_left_symm _ _ := rfl
      homEquiv_naturality_right _ _ := rfl }

/-! ### Models of theories -/

/-- The models of a set of sentences, as a property of objects of `Mod Σ`. -/
def modelsProperty {S : Sign} (E : Set (I.obj S).Sen) : ObjectProperty (I.obj S).Mod :=
  fun M => ∀ φ ∈ E, (I.obj S).Sat M φ

/-- The category `Mod(T)` (also `T*`) of models of a theory: the full subcategory of `Mod Σ`
on the models satisfying every sentence of `T`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, after Definition 5. -/
abbrev Models (T : Theory I) : Type m₂ := (modelsProperty T.sentences).FullSubcategory

/-- Reducts of models along a theory morphism satisfy the source theory.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, the argument after
Definition 6 (by the Satisfaction Condition). -/
theorem reduct_mem {T T' : Theory I} (f : T ⟶ T') (M : T'.Models) :
    modelsProperty T.sentences ((I.map f.hom.sig).mod.obj M.obj) :=
  fun φ hφ => ((I.map f.hom.sig).sat M.obj φ).1 (M.property _ (f.hom.mapsTo hφ))

/-- The forgetful (reduct) functor `σ* : Mod(T') ⥤ Mod(T)` of a theory morphism.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 6. -/
def reduct {T T' : Theory I} (f : T ⟶ T') : T'.Models ⥤ T.Models :=
  ObjectProperty.lift _ (ObjectProperty.ι _ ⋙ (I.map f.hom.sig).mod) (reduct_mem f)

/-! ### The institution of theories -/

variable (I) in
/-- The room of a theory: its signature's sentences, its category of models, and the restricted
satisfaction relation.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §5, Definition 5.1
(the theoroidal institution `Iᵗʰ`). -/
def room (T : Theory I) : Room.{w, m₁, m₂} :=
  ⟨(I.obj T.sig).Sen, Cat.of T.Models, fun (M : T.Models) φ => (I.obj T.sig).Sat M.obj φ⟩

variable (I) in
/-- The corridor of a theory morphism. -/
def corridor {T T' : Theory I} (f : T ⟶ T') : room I T ⟶ room I T' :=
  ⟨(I.map f.hom.sig).sen, reduct f, fun (M : T'.Models) φ => (I.map f.hom.sig).sat M.obj φ⟩

/-- Lifting the identity functor to a full subcategory gives the identity. -/
theorem lift_ι_comp_eq_id {C : Type*} [Category C] (P : ObjectProperty C) (F : C ⥤ C)
    (hF : F = 𝟭 C) (h : ∀ X : P.FullSubcategory, P ((P.ι ⋙ F).obj X)) :
    P.lift (P.ι ⋙ F) h = 𝟭 P.FullSubcategory := by
  subst hF
  rfl

/-- Lifts of composable functors to full subcategories compose. -/
theorem lift_ι_comp_eq_comp {C D E : Type*} [Category C] [Category D] [Category E]
    (P : ObjectProperty C) (Q : ObjectProperty D) (R : ObjectProperty E) (G : E ⥤ D)
    (F : D ⥤ C) (H : E ⥤ C) (hH : H = G ⋙ F)
    (h : ∀ X : R.FullSubcategory, P ((R.ι ⋙ H).obj X))
    (hG : ∀ X : R.FullSubcategory, Q ((R.ι ⋙ G).obj X))
    (hF : ∀ X : Q.FullSubcategory, P ((Q.ι ⋙ F).obj X)) :
    P.lift (R.ι ⋙ H) h = Q.lift (R.ι ⋙ G) hG ⋙ P.lift (Q.ι ⋙ F) hF := by
  subst hH
  rfl

theorem corridor_id (T : Theory I) : corridor I (𝟙 T) = 𝟙 (room I T) := by
  apply Room.hom_ext
  · change (I.map (𝟙 T.sig)).sen = id
    rw [I.map_id]
    rfl
  · exact lift_ι_comp_eq_id _ _ (by
      change (I.map (𝟙 T.sig)).mod = 𝟭 _
      rw [I.map_id]
      rfl) _

theorem corridor_comp {T T' T'' : Theory I} (f : T ⟶ T') (g : T' ⟶ T'') :
    corridor I (f ≫ g) = corridor I f ≫ corridor I g := by
  apply Room.hom_ext
  · change (I.map (f.hom.sig ≫ g.hom.sig)).sen =
      (I.map g.hom.sig).sen ∘ (I.map f.hom.sig).sen
    rw [I.map_comp]
    rfl
  · exact lift_ι_comp_eq_comp _ _ _ _ _ _ (by
      change (I.map (f.hom.sig ≫ g.hom.sig)).mod =
        (I.map g.hom.sig).mod ⋙ (I.map f.hom.sig).mod
      rw [I.map_comp]
      rfl) _ _ _

variable (I) in
/-- The institution `Iᵗʰ` whose signatures are the theories of `I`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §5, Definition 5.1
(the theoroidal institution `Iᵗʰ`); `goguen-burstall-1992` in `references.bib`, §2.2,
Definition 6 (the model functor on theories). -/
def institution : Theory I ⥤ Room.{w, m₁, m₂} where
  obj := room I
  map := corridor I
  map_id := corridor_id
  map_comp := corridor_comp

end Theory

/-! ### Goguen–Roşu's `Spec`: specifications with semantic morphisms -/

/-- A specification `(Σ, F)` in the sense of Goguen–Roşu: a signature with a set of sentences.
Its objects are those of `Presentation I`; it differs in its morphisms (`Spec.Hom`).

Source: Goguen–Roşu 2002, §3.1, Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
structure Spec where
  /-- The signature `Σ`. -/
  sig : Sign
  /-- The sentences `F ⊆ Sen Σ`. -/
  axioms : Set (I.obj sig).Sen

namespace Spec

variable {I}

/-- A specification morphism `(Σ, F) → (Σ', F')`: a signature morphism `φ` with `φ(F) ⊆ F'•`,
the closure of `F'` (not `F'` itself; compare the syntactic `Presentation.Hom`).

Source: Goguen–Roşu 2002, §3.1, Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
@[ext]
structure Hom (P Q : Spec I) where
  /-- The underlying signature morphism. -/
  sig : P.sig ⟶ Q.sig
  /-- `φ(F) ⊆ F'•`. -/
  mapsTo : Set.MapsTo (I.map sig).sen P.axioms ((I.obj Q.sig).closure Q.axioms)

theorem mapsTo_id (P : Spec I) :
    Set.MapsTo (I.map (𝟙 P.sig)).sen P.axioms ((I.obj P.sig).closure P.axioms) := by
  (intro φ hφ; rw [I.map_id]; exact (I.obj P.sig).subset_closure P.axioms hφ)

/-- Specification morphisms compose: if `φ(F) ⊆ F'•` and `ψ(F') ⊆ F''•` then
`ψ(φ(F)) ⊆ ψ(F'•) ⊆ F''•`, the last step by the Presentation Lemma.

Source: `goguen-burstall-1992` in `references.bib`, §2.3, Lemma 8. -/
theorem Hom.mapsTo_comp {P Q R : Spec I} (f : Hom P Q) (g : Hom Q R) :
    Set.MapsTo (I.map (f.sig ≫ g.sig)).sen P.axioms ((I.obj R.sig).closure R.axioms) := by
  intro φ hφ
  rw [I.map_comp]
  exact (Theory.presentation_lemma g.sig Q.axioms R.axioms).2 g.mapsTo (f.mapsTo hφ)

/-- The category `Spec` of specifications and specification morphisms.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
instance category : Category.{v} (Spec I) where
  Hom := Hom
  id P := ⟨𝟙 P.sig, mapsTo_id P⟩
  comp f g := ⟨f.sig ≫ g.sig, f.mapsTo_comp g⟩
  id_comp f := Hom.ext (Category.id_comp f.sig)
  comp_id f := Hom.ext (Category.comp_id f.sig)
  assoc f g h := Hom.ext (Category.assoc f.sig g.sig h.sig)

@[ext]
theorem hom_ext {P Q : Spec I} {f g : P ⟶ Q} (h : f.sig = g.sig) : f = g := Hom.ext h

@[simp]
theorem id_sig (P : Spec I) : Hom.sig (𝟙 P) = 𝟙 P.sig := rfl

@[simp]
theorem comp_sig {P Q R : Spec I} (f : P ⟶ Q) (g : Q ⟶ R) :
    Hom.sig (f ≫ g) = f.sig ≫ g.sig := rfl

variable (I) in
/-- The forgetful functor `Spec ⥤ Sign`. -/
def forget : Spec I ⥤ Sign where
  obj P := P.sig
  map f := f.sig

/-- Every syntactic presentation morphism (`σ(E) ⊆ E'`) is a specification morphism
(`σ(E) ⊆ E'•`). -/
def ofPresentationHom {P Q : Presentation I} (f : P ⟶ Q) :
    (⟨P.sig, P.axioms⟩ : Spec I) ⟶ ⟨Q.sig, Q.axioms⟩ :=
  ⟨f.sig, fun _ hφ => (I.obj Q.sig).subset_closure Q.axioms (f.mapsTo hφ)⟩

variable (I) in
/-- The closure functor `F : Spec ⥤ Th`, `(Σ, F) ↦ (Σ, F•)`, identity on morphisms.

Source: Goguen–Roşu 2002, §3.1, the paragraph after Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8;
`goguen-burstall-1992` in `references.bib`, §2.3, Lemma 8. -/
def closure : Spec I ⥤ Theory I where
  obj P := ⟨⟨P.sig, (I.obj P.sig).closure P.axioms⟩, (I.obj P.sig).closure_isClosed P.axioms⟩
  map f := ObjectProperty.homMk
    ⟨f.sig, (Theory.presentation_lemma f.sig _ _).2 f.mapsTo⟩
  map_id _ := rfl
  map_comp _ _ := rfl

end Spec

/-- The inclusion functor `U : Th ⥤ Spec`.

Source: Goguen–Roşu 2002, §3.1, the paragraph after Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
def Theory.toSpec : Theory I ⥤ Spec I where
  obj T := ⟨T.sig, T.sentences⟩
  map f := ⟨f.hom.sig, fun _ hφ => (I.obj _).subset_closure _ (f.hom.mapsTo hφ)⟩
  map_id _ := rfl
  map_comp _ _ := rfl

namespace Spec

variable {I}

/-- The identity signature morphism maps `A` into `B` whenever `A ⊆ B`. -/
theorem mapsTo_map_id {S : Sign} {A B : Set (I.obj S).Sen} (h : A ⊆ B) :
    Set.MapsTo (I.map (𝟙 S)).sen A B := by
  intro φ hφ
  rw [I.map_id]
  exact h hφ

/-- A specification is isomorphic in `Spec` to its closure: `(Σ, F) ≅ (Σ, F•)` via identities.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4 and the paragraph after it,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
def isoClosure (P : Spec I) : P ≅ (Theory.toSpec I).obj ((closure I).obj P) where
  hom := ⟨𝟙 P.sig, mapsTo_map_id fun _ hφ =>
    (I.obj P.sig).subset_closure _ ((I.obj P.sig).subset_closure _ hφ)⟩
  inv := ⟨𝟙 P.sig, mapsTo_map_id fun _ hφ => hφ⟩
  hom_inv_id := hom_ext (Category.comp_id _)
  inv_hom_id := hom_ext (Category.comp_id _)

/-- A theory is isomorphic in `Th` to the closure of its underlying specification
(`T• = T`). -/
def closureIso (T : Theory I) : (closure I).obj ((Theory.toSpec I).obj T) ≅ T where
  hom := ObjectProperty.homMk ⟨𝟙 T.sig, mapsTo_map_id (by
    have h : (I.obj T.sig).closure T.sentences = T.sentences := T.isClosed
    exact h.subset)⟩
  inv := ObjectProperty.homMk ⟨𝟙 T.sig, mapsTo_map_id ((I.obj T.sig).subset_closure _)⟩
  hom_inv_id := Theory.hom_ext (Category.comp_id _)
  inv_hom_id := Theory.hom_ext (Category.comp_id _)

variable (I) in
/-- Goguen–Roşu's equivalence `Spec ≌ Th`: the closure functor `F` and the inclusion
`U : Th ⥤ Spec` are mutually quasi-inverse.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4 and the paragraph after it ("The inclusion
functor U : Th → Spec is an equivalence of categories"),
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
def equivalence : Spec I ≌ Theory I where
  functor := closure I
  inverse := Theory.toSpec I
  unitIso := NatIso.ofComponents isoClosure fun _ =>
    hom_ext ((Category.comp_id _).trans (Category.id_comp _).symm)
  counitIso := NatIso.ofComponents closureIso fun _ =>
    Theory.hom_ext ((Category.comp_id _).trans (Category.id_comp _).symm)
  functor_unitIso_comp _ := Theory.hom_ext (Category.comp_id _)

/-- The inclusion `U : Th ⥤ Spec` is an equivalence of categories.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4 and the paragraph after it,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
instance toSpec_isEquivalence : (Theory.toSpec I).IsEquivalence :=
  (equivalence I).isEquivalence_inverse

/-- The closure functor `F : Spec ⥤ Th` is an equivalence of categories. -/
instance closure_isEquivalence : (closure I).IsEquivalence :=
  (equivalence I).isEquivalence_functor

variable (I) in
/-- Closure is left adjoint to the inclusion, `F ⊣ U`: `Th` is reflective in `Spec`.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4 and the paragraph after it ("a
left-adjoint-left-inverse F"), `goguen-rosu-2002-institution-morphisms` in `references.bib`
p. 8. -/
def closureAdjunction : closure I ⊣ Theory.toSpec I := (equivalence I).toAdjunction

variable (I) in
/-- Closure is also right adjoint to the inclusion, `U ⊣ F`: `Th` is coreflective in `Spec`.

Source: Goguen–Roşu 2002, §3.1, Definition 3.4 and the paragraph after it ("F is also a right
adjoint of U, so that Th is a reflective and coreflective subcategory of Spec"),
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
def inclusionAdjunction : Theory.toSpec I ⊣ closure I := (equivalence I).symm.toAdjunction

/-- `Th` is a reflective subcategory of `Spec`.

Source: Goguen–Roşu 2002, §3.1, the paragraph after Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
instance reflective : Reflective (Theory.toSpec I) where
  L := closure I
  adj := closureAdjunction I

/-- `Th` is a coreflective subcategory of `Spec`.

Source: Goguen–Roşu 2002, §3.1, the paragraph after Definition 3.4,
`goguen-rosu-2002-institution-morphisms` in `references.bib` p. 8. -/
instance coreflective : Coreflective (Theory.toSpec I) where
  R := closure I
  adj := inclusionAdjunction I

/-- The closure of a specification is its closure in the equivalence: the theory presented by
`(Σ, F)` under the syntactic `Theory.ofPresentation` and under `Spec.closure` agree. -/
theorem closure_obj_eq_ofPresentation_obj (P : Presentation I) :
    (closure I).obj ⟨P.sig, P.axioms⟩ = (Theory.ofPresentation I).obj P := rfl

end Spec

/-! ### Consequence along a single signature morphism -/

section Consequence

variable {I}

/-- Semantic consequence is preserved by translation along a signature morphism:
`Γ ⊨ φ` implies `σ(Γ) ⊨ σ(φ)`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.7(1); `goguen-burstall-1992` in `references.bib`, §2.3 (after
Lemma 7, "E ⊨ e implies ΦE ⊨ Φe"). -/
theorem entails_map {S S' : Sign} (σ : S ⟶ S') {Γ : Set (I.obj S).Sen} {φ : (I.obj S).Sen}
    (h : (I.obj S).Entails Γ φ) : (I.obj S').Entails ((I.map σ).sen '' Γ) ((I.map σ).sen φ) :=
  (I.map σ).entails_map h

/-- A signature morphism is conservative when every `Σ`-model is the reduct of a `Σ'`-model.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Definition 3.6. -/
def Conservative {S S' : Sign} (σ : S ⟶ S') : Prop := Corridor.ModelExpansive (I.map σ)

/-- Conservative signature morphisms reflect consequence: `Γ ⊨ φ ↔ σ(Γ) ⊨ σ(φ)`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.7(2). -/
theorem entails_map_iff {S S' : Sign} {σ : S ⟶ S'} (hσ : Conservative (I := I) σ)
    (Γ : Set (I.obj S).Sen) (φ : (I.obj S).Sen) :
    (I.obj S).Entails Γ φ ↔ (I.obj S').Entails ((I.map σ).sen '' Γ) ((I.map σ).sen φ) :=
  (I.map σ).entails_map_iff hσ Γ φ

end Consequence

end Silmaril.Logic.Institution
