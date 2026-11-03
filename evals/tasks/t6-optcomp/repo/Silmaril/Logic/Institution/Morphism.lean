module

public import Silmaril.Logic.Institution.Theory
public import Mathlib.CategoryTheory.Adjunction.Whiskering

@[expose] public section

/-!
# Institution morphisms and comorphisms

With institutions as room functors, the two classical notions of map between institutions
`I : Sign ⥤ Room` and `I' : Sign' ⥤ Room` are a signature functor together with a natural
transformation of room functors, in one of two directions:

* an *institution morphism* (Goguen–Burstall) `(Φ, η) : I → I'` has `Φ : Sign ⥤ Sign'` and
  `η : Φ ⋙ I' ⟶ I`; each component is a corridor `I'(ΦΣ) → I(Σ)`, translating `I'`-sentences to
  `I`-sentences and `I`-models to `I'`-models;
* an *institution comorphism* (Meseguer's "plain map", Tarlecki's "representation") `(Φ, ρ) :
  I → I'` has `Φ : Sign ⥤ Sign'` and `ρ : I ⟶ Φ ⋙ I'`; each component is a corridor
  `I(Σ) → I'(ΦΣ)`, translating sentences forwards and models backwards.

The satisfaction conditions of Goguen–Roşu's Definitions 4.1 and 4.2 are the satisfaction
conditions of these corridors, and their naturality conditions are the naturality of `η` and `ρ`.
`Ins` and `CoIns` are the resulting categories (Goguen–Roşu's `INS` and `coINS`).

When `Φ : Sign ⥤ Sign'` has a left adjoint `Φ'`, morphisms with signature functor `Φ` correspond
bijectively to comorphisms with signature functor `Φ'` (`morphismEquivComorphism`, Goguen–Roşu
Theorem 4.11, after Arrais–Fiadeiro). In the room view this is literally Mathlib's lifting of the
adjunction `Φ' ⊣ Φ` to functor categories by precomposition.
-/

namespace Silmaril.Logic.Institution

open CategoryTheory

universe u v w m₁ m₂ u₁ v₁ u₂ v₂ u₃ v₃

/-- A bundled institution: a category of signatures and a room functor.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 12;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4,
Definition 4.2. -/
structure Inst where
  /-- The category of signatures. -/
  Sign : Type u
  [cat : Category.{v} Sign]
  /-- The institution as a functor into rooms. -/
  I : Sign ⥤ Room.{w, m₁, m₂}

attribute [instance] Inst.cat

namespace Inst

/-- An institution morphism `(Φ, η) : A → B` with `η : Φ ⋙ B ⟶ A`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 13 (and its
concrete form, §4.1, Definition 32); `goguen-rosu-2002-institution-morphisms` in `references.bib`,
§4, Definition 4.1. -/
structure Hom (A : Inst.{u₁, v₁, w, m₁, m₂}) (B : Inst.{u₂, v₂, w, m₁, m₂}) where
  /-- The signature functor `Φ : Sign_A ⥤ Sign_B`. -/
  Φ : A.Sign ⥤ B.Sign
  /-- The natural transformation `η : Φ ⋙ I_B ⟶ I_A`; its components translate `B`-sentences
  to `A`-sentences and `A`-models to `B`-models. -/
  η : Φ ⋙ B.I ⟶ A.I

/-- An institution comorphism `(Φ, ρ) : A → B` with `ρ : A ⟶ Φ ⋙ B`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.2;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4.1,
Definition 4.3. -/
structure CoHom (A : Inst.{u₁, v₁, w, m₁, m₂}) (B : Inst.{u₂, v₂, w, m₁, m₂}) where
  /-- The signature functor `Φ : Sign_A ⥤ Sign_B`. -/
  Φ : A.Sign ⥤ B.Sign
  /-- The natural transformation `ρ : I_A ⟶ Φ ⋙ I_B`; its components are corridors
  `I_A(Σ) → I_B(ΦΣ)`. -/
  ρ : A.I ⟶ Φ ⋙ B.I

variable {A : Inst.{u₁, v₁, w, m₁, m₂}} {B : Inst.{u₂, v₂, w, m₁, m₂}}
  {C : Inst.{u₃, v₃, w, m₁, m₂}}

/-- The satisfaction condition of an institution morphism, as in Goguen–Roşu Definition 4.1:
`m ⊨_Σ η_Σ(f') ↔ η_Σ(m) ⊨'_{ΦΣ} f'`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.1;
`goguen-burstall-1992` in `references.bib`, §4.1, Definition 32. -/
theorem Hom.satisfaction (f : Hom A B) (S : A.Sign) (M : (A.I.obj S).Mod)
    (φ : (B.I.obj (f.Φ.obj S)).Sen) :
    (A.I.obj S).Sat M ((f.η.app S).sen φ) ↔
      (B.I.obj (f.Φ.obj S)).Sat ((f.η.app S).mod.obj M) φ :=
  (f.η.app S).sat M φ

/-- The satisfaction condition of an institution comorphism, as in Goguen–Roşu Definition 4.2:
`ρ_Σ(m') ⊨_Σ f ↔ m' ⊨'_{ΦΣ} ρ_Σ(f)`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.2;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4.1,
Definition 4.3. -/
theorem CoHom.satisfaction (f : CoHom A B) (S : A.Sign) (M' : (B.I.obj (f.Φ.obj S)).Mod)
    (φ : (A.I.obj S).Sen) :
    (B.I.obj (f.Φ.obj S)).Sat M' ((f.ρ.app S).sen φ) ↔
      (A.I.obj S).Sat ((f.ρ.app S).mod.obj M') φ :=
  (f.ρ.app S).sat M' φ

/-- Identity morphism. -/
def Hom.id (A : Inst.{u₁, v₁, w, m₁, m₂}) : Hom A A := ⟨𝟭 _, (Functor.leftUnitor A.I).hom⟩

/-- Composition of institution morphisms: `(Φ, η) ; (Φ', η') = (Φ ⋙ Φ', (Φ ∘ η') ; η)`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 13. -/
def Hom.comp (f : Hom A B) (g : Hom B C) : Hom A C :=
  ⟨f.Φ ⋙ g.Φ, (Functor.associator f.Φ g.Φ C.I).hom ≫ Functor.whiskerLeft f.Φ g.η ≫ f.η⟩

/-- Identity comorphism. -/
def CoHom.id (A : Inst.{u₁, v₁, w, m₁, m₂}) : CoHom A A := ⟨𝟭 _, (Functor.leftUnitor A.I).inv⟩

/-- Composition of institution comorphisms.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.2
("coINS"); `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`,
§4.1, after Definition 4.3 ("With the natural compositions and identities"). -/
def CoHom.comp (f : CoHom A B) (g : CoHom B C) : CoHom A C :=
  ⟨f.Φ ⋙ g.Φ, f.ρ ≫ Functor.whiskerLeft f.Φ g.ρ ≫ (Functor.associator f.Φ g.Φ C.I).inv⟩

/-- Morphisms are equal when their signature functors are and their transformations agree
after transport. -/
theorem Hom.ext {f g : Hom A B} (hΦ : f.Φ = g.Φ)
    (hη : f.η = eqToHom (by rw [hΦ]) ≫ g.η) : f = g := by
  cases f
  cases g
  cases hΦ
  simp only [eqToHom_refl, Category.id_comp] at hη
  cases hη
  rfl

/-- Comorphisms are equal when their signature functors are and their transformations agree
after transport. -/
theorem CoHom.ext {f g : CoHom A B} (hΦ : f.Φ = g.Φ)
    (hρ : f.ρ ≫ eqToHom (by rw [hΦ]) = g.ρ) : f = g := by
  cases f
  cases g
  cases hΦ
  simp only [eqToHom_refl, Category.comp_id] at hρ
  cases hρ
  rfl

/-- The category of signatures of an institution, as an object of `Cat`. -/
def signCat (A : Inst.{u, v, w, m₁, m₂}) : Cat.{v, u} := Cat.of A.Sign

/-- The signature functor of a morphism, as a morphism in `Cat`. -/
def Hom.sigHom {A B : Inst.{u, v, w, m₁, m₂}} (f : Hom A B) : A.signCat ⟶ B.signCat :=
  f.Φ.toCatHom

/-- The signature functor of a comorphism, as a morphism in `Cat`. -/
def CoHom.sigHom {A B : Inst.{u, v, w, m₁, m₂}} (f : CoHom A B) : A.signCat ⟶ B.signCat :=
  f.Φ.toCatHom

end Inst

set_option linter.checkUnivs false in
/-- The category `INS` of institutions and institution morphisms (a type synonym for `Inst`).

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 13;
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.1. -/
def Ins : Type _ := Inst.{u, v, w, m₁, m₂}

set_option linter.checkUnivs false in
/-- The category `coINS` of institutions and institution comorphisms (a type synonym for `Inst`).

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.2;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4.1
("CoIns"). -/
def CoIns : Type _ := Inst.{u, v, w, m₁, m₂}

/-- `INS` is a category.

Source: `goguen-burstall-1992` in `references.bib`, §2.5, Definition 13 ("Let
INS denote the category of institutions"). -/
instance Ins.category : Category Ins.{u, v, w, m₁, m₂} where
  Hom A B := Inst.Hom A B
  id A := Inst.Hom.id A
  comp f g := Inst.Hom.comp f g
  id_comp _ := rfl
  comp_id _ := rfl
  assoc _ _ _ := rfl

/-- `coINS` is a category.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4, Definition 4.2;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §4.1. -/
instance CoIns.category : Category CoIns.{u, v, w, m₁, m₂} where
  Hom A B := Inst.CoHom A B
  id A := Inst.CoHom.id A
  comp f g := Inst.CoHom.comp f g
  id_comp _ := rfl
  comp_id _ := rfl
  assoc _ _ _ := rfl

/-- The functor `INS ⥤ Cat` extracting the category of signatures of an institution.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §2.3,
Proposition 2.10 (the forgetful functor extracting the underlying "shape" category of a
diagram), via §4, Theorem 4.6 (`INS ≅ Dgm(Trel)`). -/
def Ins.signFunctor : Ins.{u, v, w, m₁, m₂} ⥤ Cat.{v, u} where
  obj A := Inst.signCat A
  map {A B} f := Inst.Hom.sigHom (A := A) (B := B) f
  map_id _ := rfl
  map_comp _ _ := rfl

/-- The functor `coINS ⥤ Cat` extracting the category of signatures of an institution.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §2.3,
Proposition 2.10, via §4, Theorem 4.6 (`coINS ≅ coDgm(Trel)`). -/
def CoIns.signFunctor : CoIns.{u, v, w, m₁, m₂} ⥤ Cat.{v, u} where
  obj A := Inst.signCat A
  map {A B} f := Inst.CoHom.sigHom (A := A) (B := B) f
  map_id _ := rfl
  map_comp _ _ := rfl

/-! ### Duality between morphisms and comorphisms -/

section Duality

variable {Sign : Type u₁} [Category.{v₁} Sign] {Sign' : Type u₂} [Category.{v₂} Sign']
  {Φ : Sign ⥤ Sign'} {Φ' : Sign' ⥤ Sign} (adj : Φ' ⊣ Φ)
  (I : Sign ⥤ Room.{w, m₁, m₂}) (I' : Sign' ⥤ Room.{w, m₁, m₂})

/-- Given an adjunction `Φ' ⊣ Φ` between signature categories, institution morphisms `I → I'`
with signature functor `Φ` (natural transformations `Φ ⋙ I' ⟶ I`) are in bijection with
institution comorphisms `I' → I` with signature functor `Φ'` (natural transformations
`I' ⟶ Φ' ⋙ I`).

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, Theorem 4.11
(via Corollary 2.8, the contravariant lifting of adjunctions to functor categories). -/
def morphismEquivComorphism : (Φ ⋙ I' ⟶ I) ≃ (I' ⟶ Φ' ⋙ I) :=
  (adj.whiskerLeft Room.{w, m₁, m₂}).homEquiv I' I

/-- The comorphism of a morphism: `ρ_{Σ'} = I'(unit_{Σ'}) ; η_{Φ'Σ'}`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, item 1 after
Theorem 4.11. -/
theorem morphismEquivComorphism_apply_app (η : Φ ⋙ I' ⟶ I) (S' : Sign') :
    (morphismEquivComorphism adj I I' η).app S' = I'.map (adj.unit.app S') ≫ η.app (Φ'.obj S') := by
  change ((adj.whiskerLeft Room.{w, m₁, m₂}).unit.app I').app S' ≫ η.app (Φ'.obj S') = _
  simp

/-- The morphism of a comorphism: `η_Σ = ρ_{ΦΣ} ; I(counit_Σ)`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, item 2 after
Theorem 4.11. -/
theorem morphismEquivComorphism_symm_apply_app (ρ : I' ⟶ Φ' ⋙ I) (S : Sign) :
    ((morphismEquivComorphism adj I I').symm ρ).app S = ρ.app (Φ.obj S) ≫ I.map (adj.counit.app S) := by
  change ρ.app (Φ.obj S) ≫ ((adj.whiskerLeft Room.{w, m₁, m₂}).counit.app I).app S = _
  simp

/-- The bijection is natural in `I` and `I'`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, Theorem 4.11
("Moreover, this bijection is natural in I and I'"). -/
theorem morphismEquivComorphism_naturality {I₀ : Sign ⥤ Room.{w, m₁, m₂}}
    {I₀' : Sign' ⥤ Room.{w, m₁, m₂}} (f' : I₀' ⟶ I') (g : I ⟶ I₀) (η : Φ ⋙ I' ⟶ I) :
    morphismEquivComorphism adj I₀ I₀' (Functor.whiskerLeft Φ f' ≫ η ≫ g) =
      f' ≫ morphismEquivComorphism adj I I' η ≫ Functor.whiskerLeft Φ' g := by
  refine NatTrans.ext (funext fun S' => ?_)
  simp [morphismEquivComorphism_apply_app]

/-- The comorphism `I' → I` corresponding to a morphism `I → I'` whose signature functor has a
left adjoint.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, Theorem 4.11. -/
def Inst.Hom.toCoHom {A B : Inst.{u, v, w, m₁, m₂}} (f : Inst.Hom A B) {Ψ : B.Sign ⥤ A.Sign}
    (adj : Ψ ⊣ f.Φ) : Inst.CoHom B A :=
  ⟨Ψ, morphismEquivComorphism adj A.I B.I f.η⟩

/-- The morphism `A → B` corresponding to a comorphism `B → A` whose signature functor has a
right adjoint.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §4.2, Theorem 4.11. -/
def Inst.CoHom.toHom {A B : Inst.{u, v, w, m₁, m₂}} (g : Inst.CoHom B A) {Ψ : A.Sign ⥤ B.Sign}
    (adj : g.Φ ⊣ Ψ) : Inst.Hom A B :=
  ⟨Ψ, (morphismEquivComorphism adj A.I B.I).symm g.ρ⟩

theorem Inst.Hom.toCoHom_toHom {A B : Inst.{u, v, w, m₁, m₂}} (f : Inst.Hom A B)
    {Ψ : B.Sign ⥤ A.Sign} (adj : Ψ ⊣ f.Φ) : (f.toCoHom adj).toHom adj = f := by
  cases f
  exact congrArg (Inst.Hom.mk _) (Equiv.symm_apply_apply _ _)

theorem Inst.CoHom.toHom_toCoHom {A B : Inst.{u, v, w, m₁, m₂}} (g : Inst.CoHom B A)
    {Ψ : A.Sign ⥤ B.Sign} (adj : g.Φ ⊣ Ψ) : (g.toHom adj).toCoHom adj = g := by
  cases g
  exact congrArg (Inst.CoHom.mk _) (Equiv.apply_symm_apply _ _)

end Duality

/-! ### Simple theoroidal comorphisms -/

/-- The bundled institution `Iᵗʰ` of theories of `A`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §5, Definition 5.1
(the theoroidal institution `Iᵗʰ`). -/
def Inst.th (A : Inst.{u, v, w, m₁, m₂}) : Inst.{max u w, v, w, m₁, m₂} :=
  ⟨Theory A.I, Theory.institution A.I⟩

/-- A simple theoroidal comorphism `A → B`: a comorphism from `A` to the institution of
theories of `B`, mapping signatures to theories.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §5.1, Definition 5.7;
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3 (simple
theoroidal corridors, footnote 11) and §4.1. -/
abbrev Inst.SimpleTheoroidalCoHom (A : Inst.{u₁, v₁, w, m₁, m₂}) (B : Inst.{u₂, v₂, w, m₁, m₂}) :=
  Inst.CoHom A B.th

end Silmaril.Logic.Institution
