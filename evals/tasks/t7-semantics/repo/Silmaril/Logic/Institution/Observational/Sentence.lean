module

public import Mathlib.Data.Set.Basic

@[expose] public section

/-! # Observational equations and refinements

A sentence compares two programs by equality or inclusion of their supplied
behavior sets. This syntax does not choose a language, observation type or
execution model. The course and other compiler semantics can reuse it.
-/

universe u

namespace Silmaril.Logic.Institution.Observational

/-- Sentences over programs `P`: observational equations and observational refinements. -/
inductive Sentence (P : Type u) : Type u where
  /-- The observational equation `a ≐ b`: the same behaviour. -/
  | eq (a b : P)
  /-- The observational refinement `a ⊑ b`: every outcome of `a` is an outcome of `b`. -/
  | le (a b : P)

namespace Sentence

variable {P Q R : Type u}

/-- Translate both sides of a sentence. -/
def map (f : P → Q) : Sentence P → Sentence Q
  | .eq a b => .eq (f a) (f b)
  | .le a b => .le (f a) (f b)

@[simp] theorem map_id : map (id : P → P) = id := by
  funext φ
  cases φ <;> rfl

theorem map_comp (f : P → Q) (g : Q → R) : map g ∘ map f = map (g ∘ f) := by
  funext φ
  cases φ <;> rfl

/-- Satisfaction of a sentence by a behaviour assignment. -/
def Holds {O : Type*} (beh : P → Set O) : Sentence P → Prop
  | .eq a b => beh a = beh b
  | .le a b => beh a ⊆ beh b

end Sentence

end Silmaril.Logic.Institution.Observational
