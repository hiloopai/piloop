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
theorem ext {f g : Corridor R₁ R₂} (h₁ : f.sen = g.sen) (h₂ : f.mod = g.mod) : f = g := sorry

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
    (h₂ : f.mod = g.mod) : f = g := sorry

@[simp] theorem id_sen : (𝟙 R : R ⟶ R).sen = id := sorry

@[simp] theorem id_mod : (𝟙 R : R ⟶ R).mod = 𝟭 R.Mod := sorry

@[simp] theorem comp_sen {R₁ R₂ R₃ : Room.{w, m₁, m₂}} (f : R₁ ⟶ R₂) (g : R₂ ⟶ R₃) :
    (f ≫ g).sen = g.sen ∘ f.sen := sorry

@[simp] theorem comp_mod {R₁ R₂ R₃ : Room.{w, m₁, m₂}} (f : R₁ ⟶ R₂) (g : R₂ ⟶ R₃) :
    (f ≫ g).mod = g.mod ⋙ f.mod := sorry

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
    R.Entails Γ φ ↔ φ ∈ R.closure Γ := sorry

/-- The two maps `*` form an (antitone) Galois connection: `Γ ⊆ 𝓜*` iff `𝓜 ⊆ Γ*`, stated with
`(Set R.Mod)ᵒᵈ` so that it is a Mathlib `GaloisConnection`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Proposition 3,
items (1)–(4). -/
theorem galoisConnection :
    GaloisConnection (fun Γ : Set R.Sen => OrderDual.toDual (R.modelsOf Γ))
      (fun 𝓜 : (Set R.Mod)ᵒᵈ => R.theoryOf (OrderDual.ofDual 𝓜)) := sorry

/-- `Γ ⊆ Γ'` implies `Γ'* ⊆ Γ*`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(1). -/
theorem modelsOf_antitone : Antitone R.modelsOf := sorry

/-- `𝓜 ⊆ 𝓜'` implies `𝓜'* ⊆ 𝓜*`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(2). -/
theorem theoryOf_antitone : Antitone R.theoryOf := sorry

/-- `Γ ⊆ Γ**`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(3). -/
theorem subset_closure (Γ : Set R.Sen) : Γ ⊆ R.closure Γ := sorry

/-- `𝓜 ⊆ 𝓜**`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(4). -/
theorem subset_modelsOf_theoryOf (𝓜 : Set R.Mod) : 𝓜 ⊆ R.modelsOf (R.theoryOf 𝓜) := sorry

/-- `Γ* = Γ***`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(5). -/
theorem modelsOf_closure (Γ : Set R.Sen) : R.modelsOf (R.closure Γ) = R.modelsOf Γ := sorry

/-- `𝓜* = 𝓜***`.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3(6). -/
theorem theoryOf_modelsOf_theoryOf (𝓜 : Set R.Mod) :
    R.theoryOf (R.modelsOf (R.theoryOf 𝓜)) = R.theoryOf 𝓜 := sorry

/-- Closure is idempotent.

Source: `goguen-burstall-1992` in `references.bib`, Proposition 3 (consequence
of (5)); `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3, footnote 3. -/
theorem closure_closure (Γ : Set R.Sen) : R.closure (R.closure Γ) = R.closure Γ := sorry

theorem closure_isClosed (Γ : Set R.Sen) : R.IsClosed (R.closure Γ) := sorry

theorem theoryOf_isClosed (𝓜 : Set R.Mod) : R.IsClosed (R.theoryOf 𝓜) := sorry

theorem modelsOf_isClosedModels (Γ : Set R.Sen) : R.IsClosedModels (R.modelsOf Γ) := sorry

/-- `(Γ• ∪ Γ')• = (Γ ∪ Γ')•`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.3(3). -/
theorem closure_closure_union (Γ Γ' : Set R.Sen) :
    R.closure (R.closure Γ ∪ Γ') = R.closure (Γ ∪ Γ') := sorry

/-- Closure as a Mathlib closure operator, induced by the Galois connection.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3 ("Closure is
obviously a closure operator", footnote 3). -/
def closureOperator : ClosureOperator (Set R.Sen) :=
  R.galoisConnection.closureOperator

theorem closureOperator_apply (Γ : Set R.Sen) : R.closureOperator Γ = R.closure Γ := sorry

/-- For closed `Γ` and `Δ`, `Δ* ⊆ Γ*` iff `Γ ⊆ Δ`. -/
theorem modelsOf_subset_modelsOf_iff {Γ Δ : Set R.Sen} (hΓ : R.IsClosed Γ)
    (hΔ : R.IsClosed Δ) : R.modelsOf Δ ⊆ R.modelsOf Γ ↔ Γ ⊆ Δ := sorry

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

/-- `(α(Γ•))• = (αΓ)•`.

Source: `goguen-rosu-2002-institution-morphisms` in `references.bib`, §3.1,
Proposition 3.3(2). -/
theorem closure_image_closure (Γ : Set R₁.Sen) :
    R₂.closure (c.sen '' R₁.closure Γ) = R₂.closure (c.sen '' Γ) := sorry

/-- A corridor is model-expansive when every source model is the reduct of a target model.

Source: `mossakowski-diaconescu-tarlecki-2009-logic-translation` in `references.bib`, §3,
Definition 3.6; called "sound" for institution morphisms in
`goguen-burstall-1992` in `references.bib`, §4.1, Definition 34. -/
def ModelExpansive : Prop := Function.Surjective c.mod.obj

/-- For a model-expansive corridor, `β((αΓ)*) = Γ*`.

Source: `goguen-burstall-1992` in `references.bib`, §2.2, Proposition 4. -/
theorem image_modelsOf (hc : ModelExpansive c) (Γ : Set R₁.Sen) :
    c.mod.obj '' R₂.modelsOf (c.sen '' Γ) = R₁.modelsOf Γ := sorry

/-- For a model-expansive corridor, `α φ ∈ (αΓ)•` iff `φ ∈ Γ•`.

Source: `goguen-burstall-1992` in `references.bib`, §4.1, Proposition 35
(the form `e'• = α⁻¹((αE')•)`). -/
theorem preimage_closure (hc : ModelExpansive c) (Γ : Set R₁.Sen) :
    c.sen ⁻¹' R₂.closure (c.sen '' Γ) = R₁.closure Γ := sorry

theorem modelExpansive_id (R : Room.{w, m₁, m₂}) : ModelExpansive (𝟙 R) := sorry

theorem ModelExpansive.comp {R₃ : Room.{w, m₁, m₂}} {c : R₁ ⟶ R₂} {d : R₂ ⟶ R₃}
    (hc : ModelExpansive c) (hd : ModelExpansive d) : ModelExpansive (c ≫ d) := sorry

end Corridor

end Silmaril.Logic.Institution
