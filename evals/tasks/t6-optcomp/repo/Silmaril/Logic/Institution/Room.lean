module

public import Mathlib.CategoryTheory.Category.Cat
public import Mathlib.Order.Closure

@[expose] public section

/-!
# Rooms and corridors

A *room* `(S, M, ⊨)` is a set of sentences `S`, a category of models `M` and a satisfaction
relation `⊨ ⊆ |M| × S`. A *corridor* `(α, β) : R₁ → R₂` translates sentences forwards,
`α : S₁ → S₂`, and models backwards, `β : M₂ ⥤ M₁`, so that

  `M₂ ⊨₂ α φ ↔ β M₂ ⊨₁ φ`.

Rooms and corridors form the category `Room`. The terminology is that of Mossakowski,
Diaconescu and Tarlecki (`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`,
§3, Definitions 3.1 and 3.4); Goguen and Burstall call the same category `Trel`, the category of
"twisted relations" in its category-of-models variant
(`goguen-burstall-1992` in `references.bib`, §2.5, the paragraph after
Definition 12), and Goguen and Roşu call it `Trel` as well
(`goguen-rosu-2002-institution-morphisms` in `references.bib`, §2.4, Definition 2.11).
Forgetting model morphisms, a room is a Chu space over `Prop` and a corridor is a Chu transform
(see `Silmaril.Logic.Institution.Chu`).

An institution is then a functor `Sign ⥤ Room` (`Silmaril.Logic.Institution.Basic`). Everything
about a single signature — semantic consequence, the Galois connection between sentences and
models, closure, preservation and reflection of consequence along a translation — is a statement
about one room or one corridor, and lives in this file.

## Main definitions

* `Room`, `Corridor`, and the category instance on `Room`.
* `Room.modelsOf Γ` (`Γ*`), `Room.theoryOf 𝓜` (`𝓜*`), `Room.closure Γ` (`Γ•`),
  `Room.Entails Γ φ` (`Γ ⊨ φ`).
* `Room.closureOperator`: closure as a Mathlib `ClosureOperator`.
* `Room.closedOrderIso`: closed sentence sets are dually isomorphic to closed model classes.
* `Corridor.ModelExpansive`: the model translation is surjective on objects.
* `Room.senFunctor`, `Room.modFunctor`: Goguen–Burstall's `Right` and `Left`.

## Main results

* `Room.galoisConnection`: `Γ ⊆ 𝓜* ↔ 𝓜 ⊆ Γ*`.
* `Corridor.image_closure_subset`: the closure lemma `α(Γ•) ⊆ (αΓ)•`.
* `Corridor.entails_map`: corridors preserve semantic consequence.
* `Corridor.entails_map_iff`: model-expansive corridors also reflect it (borrowing for rooms).
-/

namespace Silmaril.Logic.Institution

open CategoryTheory

universe w m₁ m₂

/-- A room: sentences, a category of models, and satisfaction.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Definition 3.1 (with the category of models of footnote 7); the objects of the category `Trel`
of `goguen-burstall-1992` in `references.bib`, §2.5 (category variant after
Definition 12) and `goguen-rosu-2002-institution-morphisms` in `references.bib`, §2.4,
Definition 2.11. -/
structure Room : Type (max (w + 1) (m₁ + 1) (m₂ + 1)) where
  /-- The sentences of the room. -/
  Sen : Type w
  /-- The category of models of the room. -/
  Mod : Cat.{m₁, m₂}
  /-- Satisfaction `M ⊨ φ`. -/
  Sat : Mod → Sen → Prop

/-- A corridor `R₁ → R₂`: sentences translate forwards, models backwards, and satisfaction is
invariant.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Definition 3.4 (with the functor of footnote 10); the morphisms `(F, g)` of `Trel` in
`goguen-burstall-1992` in `references.bib`, §2.5, and
`goguen-rosu-2002-institution-morphisms` in `references.bib`, Definition 2.11. -/
structure Corridor (R₁ R₂ : Room.{w, m₁, m₂}) : Type (max w m₁ m₂) where
  /-- Sentence translation `α : S₁ → S₂`. -/
  sen : R₁.Sen → R₂.Sen
  /-- Model reduction `β : M₂ ⥤ M₁`. -/
  mod : R₂.Mod ⥤ R₁.Mod
  /-- The satisfaction condition of the corridor: `M₂ ⊨₂ α φ ↔ β M₂ ⊨₁ φ`. -/
  sat : ∀ (M : R₂.Mod) (φ : R₁.Sen), R₂.Sat M (sen φ) ↔ R₁.Sat (mod.obj M) φ

namespace Corridor

variable {R₁ R₂ : Room.{w, m₁, m₂}}

/-- Corridors are determined by their sentence and model translations. -/
@[ext]
theorem ext {f g : Corridor R₁ R₂} (h₁ : f.sen = g.sen) (h₂ : f.mod = g.mod) : f = g := by
  cases f
  cases g
  cases h₁
  cases h₂
  rfl

end Corridor

/-- Rooms and corridors form a category, with composition of sentence translations and
(reversed) composition of model functors.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
after Definition 3.4 ("rooms and corridors form a category Room"); composition as in `Trel`,
`goguen-burstall-1992` in `references.bib`, §2.5. -/
instance Room.category : Category.{max w m₁ m₂} Room.{w, m₁, m₂} where
  Hom := Corridor
  id _ := ⟨id, 𝟭 _, fun _ _ => Iff.rfl⟩
  comp f g := ⟨g.sen ∘ f.sen, g.mod ⋙ f.mod,
    fun M φ => (g.sat M (f.sen φ)).trans (f.sat (g.mod.obj M) φ)⟩
  id_comp _ := rfl
  comp_id _ := rfl
  assoc _ _ _ := rfl

namespace Room

variable (R : Room.{w, m₁, m₂})

@[ext]
theorem hom_ext {R₁ R₂ : Room.{w, m₁, m₂}} {f g : R₁ ⟶ R₂} (h₁ : f.sen = g.sen)
    (h₂ : f.mod = g.mod) : f = g :=
  Corridor.ext h₁ h₂

@[simp] theorem id_sen : (𝟙 R : R ⟶ R).sen = id := rfl

@[simp] theorem id_mod : (𝟙 R : R ⟶ R).mod = 𝟭 R.Mod := rfl

@[simp] theorem comp_sen {R₁ R₂ R₃ : Room.{w, m₁, m₂}} (f : R₁ ⟶ R₂) (g : R₂ ⟶ R₃) :
    (f ≫ g).sen = g.sen ∘ f.sen := rfl

@[simp] theorem comp_mod {R₁ R₂ R₃ : Room.{w, m₁, m₂}} (f : R₁ ⟶ R₂) (g : R₂ ⟶ R₃) :
    (f ≫ g).mod = g.mod ⋙ f.mod := rfl

/-! ### Semantic consequence and the Galois connection -/

/-- `Γ*`: the models satisfying every sentence of `Γ`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(3). -/
def modelsOf (Γ : Set R.Sen) : Set R.Mod := {M | ∀ φ ∈ Γ, R.Sat M φ}

/-- `𝓜*`: the sentences satisfied by every model of `𝓜`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(4). -/
def theoryOf (𝓜 : Set R.Mod) : Set R.Sen := {φ | ∀ M ∈ 𝓜, R.Sat M φ}

/-- The closure `Γ• = Γ**` of a set of sentences.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(5). -/
def closure (Γ : Set R.Sen) : Set R.Sen := R.theoryOf (R.modelsOf Γ)

/-- A set of sentences is closed when it equals its closure.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(6). -/
def IsClosed (Γ : Set R.Sen) : Prop := R.closure Γ = Γ

/-- A class of models is closed when it is the class of models of its theory (a "variety").

Source: `goguen-burstall-1992` in `references.bib`, §2.2, the paragraph after
Definition 2. -/
def IsClosedModels (𝓜 : Set R.Mod) : Prop := R.modelsOf (R.theoryOf 𝓜) = 𝓜

/-- Semantic consequence `Γ ⊨ φ`: every model of `Γ` satisfies `φ`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(9);
`mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3, after
Definition 3.1. -/
def Entails (Γ : Set R.Sen) (φ : R.Sen) : Prop :=
  ∀ M : R.Mod, (∀ ψ ∈ Γ, R.Sat M ψ) → R.Sat M φ

/-- `Γ ⊨ φ` iff `φ ∈ Γ•`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(9). -/
theorem entails_iff_mem_closure (Γ : Set R.Sen) (φ : R.Sen) :
    R.Entails Γ φ ↔ φ ∈ R.closure Γ := by
  exact Iff.rfl

/-- The two maps `*` form an (antitone) Galois connection: `Γ ⊆ 𝓜*` iff `𝓜 ⊆ Γ*`, stated with
`(Set R.Mod)ᵒᵈ` so that it is a Mathlib `GaloisConnection`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Proposition 3,
items (1)–(4). -/
theorem galoisConnection :
    GaloisConnection (fun Γ : Set R.Sen => OrderDual.toDual (R.modelsOf Γ))
      (fun 𝓜 : (Set R.Mod)ᵒᵈ => R.theoryOf (OrderDual.ofDual 𝓜)) := by
  intro Γ 𝓜
  exact ⟨fun h φ hφ M hM => h hM φ hφ, fun h M hM φ hφ => h hφ M hM⟩

/-- `Γ ⊆ Γ'` implies `Γ'* ⊆ Γ*`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(1). -/
theorem modelsOf_antitone : Antitone R.modelsOf := by
  exact fun _ _ h _ hM φ hφ => hM φ (h hφ)

/-- `𝓜 ⊆ 𝓜'` implies `𝓜'* ⊆ 𝓜*`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(2). -/
theorem theoryOf_antitone : Antitone R.theoryOf := by
  exact fun _ _ h _ hφ M hM => hφ M (h hM)

/-- `Γ ⊆ Γ**`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(3). -/
theorem subset_closure (Γ : Set R.Sen) : Γ ⊆ R.closure Γ := by
  exact fun φ hφ _ hM => hM φ hφ

/-- `𝓜 ⊆ 𝓜**`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(4). -/
theorem subset_modelsOf_theoryOf (𝓜 : Set R.Mod) : 𝓜 ⊆ R.modelsOf (R.theoryOf 𝓜) := by
  exact fun M hM _ hφ => hφ M hM

/-- `Γ* = Γ***`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(5). -/
theorem modelsOf_closure (Γ : Set R.Sen) : R.modelsOf (R.closure Γ) = R.modelsOf Γ := by
  exact Set.Subset.antisymm (R.modelsOf_antitone (R.subset_closure Γ))
    (fun M hM _ hφ => hφ M hM)

/-- `𝓜* = 𝓜***`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(6). -/
theorem theoryOf_modelsOf_theoryOf (𝓜 : Set R.Mod) :
    R.theoryOf (R.modelsOf (R.theoryOf 𝓜)) = R.theoryOf 𝓜 := by
  exact Set.Subset.antisymm (R.theoryOf_antitone (R.subset_modelsOf_theoryOf 𝓜))
    (fun φ hφ _ hM => hM φ hφ)

theorem closure_mono : Monotone R.closure := by
  exact fun _ _ h => R.theoryOf_antitone (R.modelsOf_antitone h)

/-- Closure is idempotent.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3 (consequence
of (5)); `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3, footnote 3. -/
theorem closure_closure (Γ : Set R.Sen) : R.closure (R.closure Γ) = R.closure Γ := by
  show R.theoryOf (R.modelsOf (R.closure Γ)) = R.theoryOf (R.modelsOf Γ)
  rw [R.modelsOf_closure]

theorem closure_isClosed (Γ : Set R.Sen) : R.IsClosed (R.closure Γ) :=
  R.closure_closure Γ

theorem theoryOf_isClosed (𝓜 : Set R.Mod) : R.IsClosed (R.theoryOf 𝓜) :=
  R.theoryOf_modelsOf_theoryOf 𝓜

theorem modelsOf_isClosedModels (Γ : Set R.Sen) : R.IsClosedModels (R.modelsOf Γ) :=
  R.modelsOf_closure Γ

/-- `(Γ ∪ Γ')* = Γ* ∩ Γ'*`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Definition 2(3)
(immediate from the definition of `Γ*`). -/
theorem modelsOf_union (Γ Γ' : Set R.Sen) :
    R.modelsOf (Γ ∪ Γ') = R.modelsOf Γ ∩ R.modelsOf Γ' := by
  ext M
  exact ⟨fun h => ⟨fun φ hφ => h φ (Or.inl hφ), fun φ hφ => h φ (Or.inr hφ)⟩,
    fun h φ hφ => hφ.elim (h.1 φ) (h.2 φ)⟩

/-- `(Γ• ∪ Γ')• = (Γ ∪ Γ')•`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.3(3). -/
theorem closure_closure_union (Γ Γ' : Set R.Sen) :
    R.closure (R.closure Γ ∪ Γ') = R.closure (Γ ∪ Γ') := by
  show R.theoryOf (R.modelsOf (R.closure Γ ∪ Γ')) = R.theoryOf (R.modelsOf (Γ ∪ Γ'))
  rw [R.modelsOf_union, R.modelsOf_union, R.modelsOf_closure]

/-- Closure as a Mathlib closure operator, induced by the Galois connection.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3 ("Closure is
obviously a closure operator", footnote 3). -/
def closureOperator : ClosureOperator (Set R.Sen) :=
  R.galoisConnection.closureOperator

theorem closureOperator_apply (Γ : Set R.Sen) : R.closureOperator Γ = R.closure Γ := rfl

/-- For closed `Γ` and `Δ`, `Δ* ⊆ Γ*` iff `Γ ⊆ Δ`. -/
theorem modelsOf_subset_modelsOf_iff {Γ Δ : Set R.Sen} (hΓ : R.IsClosed Γ)
    (hΔ : R.IsClosed Δ) : R.modelsOf Δ ⊆ R.modelsOf Γ ↔ Γ ⊆ Δ := by
  constructor
  · intro h
    rw [← hΓ, ← hΔ]
    exact R.theoryOf_antitone h
  · intro h
    exact R.modelsOf_antitone h

/-- The dual isomorphism between closed sets of sentences and closed classes of models.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Proposition 3(7). -/
def closedOrderIso :
    {Γ : Set R.Sen // R.IsClosed Γ} ≃o {𝓜 : Set R.Mod // R.IsClosedModels 𝓜}ᵒᵈ where
  toFun Γ := OrderDual.toDual ⟨R.modelsOf Γ.1, R.modelsOf_isClosedModels Γ.1⟩
  invFun 𝓜 := ⟨R.theoryOf (OrderDual.ofDual 𝓜).1, R.theoryOf_isClosed _⟩
  left_inv Γ := Subtype.ext Γ.2
  right_inv 𝓜 := by
    exact congrArg OrderDual.toDual (Subtype.ext (OrderDual.ofDual 𝓜).2)
  map_rel_iff' {Γ Δ} := by
    exact R.modelsOf_subset_modelsOf_iff Γ.2 Δ.2

/-! ### Rooms as twisted relations: the functors `Left` and `Right` -/

/-- The sentence functor `Room ⥤ Type`, Goguen–Burstall's `Right`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5 (`Right : Trel → Set`,
before Definition 12). -/
def senFunctor : Room.{w, m₁, m₂} ⥤ Type w where
  obj R := R.Sen
  map f := TypeCat.ofHom f.sen
  map_id _ := rfl
  map_comp _ _ := rfl

/-- The model functor `Room ⥤ Catᵒᵖ`, Goguen–Burstall's `Left`.

Source: `goguen-burstall-1992` in `references.bib`, §2.5 (`Left : Trel → Setᵒᵖ`,
before Definition 12, in its category variant). -/
def modFunctor : Room.{w, m₁, m₂} ⥤ Cat.{m₁, m₂}ᵒᵖ where
  obj R := Opposite.op R.Mod
  map {X Y} f := Quiver.Hom.op (show Y.Mod ⟶ X.Mod from Cat.Hom.ofFunctor f.mod)
  map_id _ := rfl
  map_comp _ _ := rfl

end Room

/-! ### Corridors: translation of consequence -/

namespace Corridor

variable {R₁ R₂ : Room.{w, m₁, m₂}} (c : R₁ ⟶ R₂)

/-- The satisfaction condition in set form: `β⁻¹(Γ*) = (αΓ)*`.

Source: `goguen-burstall-1992` in `references.bib`, §2.3 (the compact form of the
Satisfaction Condition before Lemma 7). -/
theorem preimage_modelsOf (Γ : Set R₁.Sen) :
    c.mod.obj ⁻¹' R₁.modelsOf Γ = R₂.modelsOf (c.sen '' Γ) := by
  ext M
  constructor
  · rintro hM _ ⟨φ, hφ, rfl⟩
    exact (c.sat M φ).2 (hM φ hφ)
  · intro hM φ hφ
    exact (c.sat M φ).1 (hM _ ⟨φ, hφ, rfl⟩)

/-- The closure lemma: `α(Γ•) ⊆ (αΓ)•`.

Source: `goguen-burstall-1992` in `references.bib`, §2.3, Lemma 7;
`goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1, Proposition 3.3(1). -/
theorem image_closure_subset (Γ : Set R₁.Sen) :
    c.sen '' R₁.closure Γ ⊆ R₂.closure (c.sen '' Γ) := by
  rintro _ ⟨φ, hφ, rfl⟩ M hM
  rw [← c.preimage_modelsOf Γ] at hM
  exact (c.sat M φ).2 (hφ _ hM)

/-- `(α(Γ•))• = (αΓ)•`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.3(2). -/
theorem closure_image_closure (Γ : Set R₁.Sen) :
    R₂.closure (c.sen '' R₁.closure Γ) = R₂.closure (c.sen '' Γ) := by
  apply Set.Subset.antisymm
  · calc R₂.closure (c.sen '' R₁.closure Γ)
          ⊆ R₂.closure (R₂.closure (c.sen '' Γ)) := R₂.closure_mono (c.image_closure_subset Γ)
      _ = R₂.closure (c.sen '' Γ) := R₂.closure_closure _
  · exact R₂.closure_mono fun _ ⟨x, hx, e⟩ => ⟨x, R₁.subset_closure Γ hx, e⟩

/-- Corridors preserve semantic consequence: `Γ ⊨ φ` implies `αΓ ⊨ αφ`.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Proposition 3.7 (first part); `goguen-rosu-2002-institution-morphisms` in `references.bib`,
§3.1, Proposition 3.7(1). -/
theorem entails_map {Γ : Set R₁.Sen} {φ : R₁.Sen} (h : R₁.Entails Γ φ) :
    R₂.Entails (c.sen '' Γ) (c.sen φ) := by
  exact c.image_closure_subset Γ ⟨φ, h, rfl⟩

/-- A corridor is model-expansive when every source model is the reduct of a target model.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Definition 3.6; called "sound" for institution morphisms in
`goguen-burstall-1992` in `references.bib`, §4.1, Definition 34. -/
def ModelExpansive : Prop := Function.Surjective c.mod.obj

/-- Model-expansive corridors reflect semantic consequence: `Γ ⊨ φ ↔ αΓ ⊨ αφ`. This is the
borrowing lemma at the level of a single room.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Proposition 3.7 (second part); `goguen-burstall-1992` in `references.bib`,
§4.1, Proposition 35 (for the corridor of a sound morphism). -/
theorem entails_map_iff (hc : ModelExpansive c) (Γ : Set R₁.Sen) (φ : R₁.Sen) :
    R₁.Entails Γ φ ↔ R₂.Entails (c.sen '' Γ) (c.sen φ) := by
  refine ⟨c.entails_map, fun h M hM => ?_⟩
  obtain ⟨N, rfl⟩ := hc M
  refine (c.sat N φ).1 (h N ?_)
  rintro _ ⟨χ, hχ, rfl⟩
  exact (c.sat N χ).2 (hM χ hχ)

/-- For a model-expansive corridor, `β((αΓ)*) = Γ*`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Proposition 4. -/
theorem image_modelsOf (hc : ModelExpansive c) (Γ : Set R₁.Sen) :
    c.mod.obj '' R₂.modelsOf (c.sen '' Γ) = R₁.modelsOf Γ := by
  rw [← c.preimage_modelsOf Γ, Set.image_preimage_eq _ hc]

/-- For a model-expansive corridor, `α φ ∈ (αΓ)•` iff `φ ∈ Γ•`.

Source: `goguen-burstall-1992` in `references.bib`, §4.1, Proposition 35
(the form `e'• = α⁻¹((αE')•)`). -/
theorem preimage_closure (hc : ModelExpansive c) (Γ : Set R₁.Sen) :
    c.sen ⁻¹' R₂.closure (c.sen '' Γ) = R₁.closure Γ := by
  ext φ
  exact (c.entails_map_iff hc Γ φ).symm

theorem modelExpansive_id (R : Room.{w, m₁, m₂}) : ModelExpansive (𝟙 R) :=
  fun M => ⟨M, rfl⟩

theorem ModelExpansive.comp {R₃ : Room.{w, m₁, m₂}} {c : R₁ ⟶ R₂} {d : R₂ ⟶ R₃}
    (hc : ModelExpansive c) (hd : ModelExpansive d) : ModelExpansive (c ≫ d) := by
  show Function.Surjective (c.mod.obj ∘ d.mod.obj)
  exact Function.Surjective.comp hc hd

end Corridor

end Silmaril.Logic.Institution
