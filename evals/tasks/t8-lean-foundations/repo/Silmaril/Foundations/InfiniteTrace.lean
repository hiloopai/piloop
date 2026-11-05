module

public import Mathlib.Logic.Relation
public import Mathlib.Order.WellFounded
public import Cslib.Foundations.Relation.Defs

/-! # Infinite paths of a relation

An infinite trace follows actual transitions. Its coinductive presentation is
equivalent to a productive post-fixed point and, classically, to failure of
accessibility for the reversed relation. None of these definitions identify
finite stuckness with divergence or require determinism.
-/

@[expose] public section

namespace Silmaril

variable {A B : Type*}

/-- An infinite sequence of actual transitions beginning at a specified state. -/
def InfiniteTrace (R : A → A → Prop) (a : A) : Prop :=
  ∃ trace : Nat → A, trace 0 = a ∧ ∀ n, R (trace n) (trace (n + 1))

/-- A forward simulation maps every transition of an infinite path. -/
theorem InfiniteTrace.map {R : A → A → Prop} {S : B → B → Prop}
    (f : A → B) (preserves : ∀ a b, R a b → S (f a) (f b)) {a : A}
    (h : InfiniteTrace R a) : InfiniteTrace S (f a) := sorry

/-- Reflect an infinite path along a map while retaining a source invariant.
Each target transition must have an actual source transition; injectivity,
determinism, and a global source progress assumption are unnecessary. -/
theorem InfiniteTrace.of_map {R : A → A → Prop} {S : B → B → Prop}
    (f : A → B) (P : A → Prop)
    (reflects : ∀ a b, P a → S (f a) b → ∃ next, R a next ∧ P next ∧ f next = b)
    {a : A} (valid : P a) (h : InfiniteTrace S (f a)) : InfiniteTrace R a := sorry

/-- The greatest-fixed-point presentation and the actual path presentation agree. -/
theorem InfiniteTrace.iff_postfixed {R : A → A → Prop} {a : A} :
    InfiniteTrace R a ↔ ∃ P : A → Prop, P a ∧ ∀ a, P a → ∃ b, R a b ∧ P b := sorry

/-- A state has no infinite trace exactly when it is strongly normalizing in
CSLib's sense. -/
theorem InfiniteTrace.not_iff_sn {R : A → A → Prop} {a : A} :
    ¬ InfiniteTrace R a ↔ Relation.SN R a := sorry

/-- A strongly normalizing state has no infinite trace. -/
theorem InfiniteTrace.not_of_sn {R : A → A → Prop} {a : A} (h : Relation.SN R a) :
    ¬ InfiniteTrace R a := sorry

/-- A context's local execution either is infinite, or reaches a boundary in
finitely many local steps while the enclosing trace continues infinitely.
The one-step inversion premise is the only context-specific obligation. -/
theorem InfiniteTrace.context_decompose (R : A → A → Prop) (S : B → B → Prop)
    (plug : A → B) (boundary : A → B → Prop)
    (peel : ∀ a b, S (plug a) b →
      (∃ next, R a next ∧ b = plug next) ∨ boundary a b)
    {initial : A} (trace : InfiniteTrace S (plug initial)) :
    InfiniteTrace R initial ∨ ∃ a b,
      Relation.ReflTransGen R initial a ∧ boundary a b ∧ InfiniteTrace S b := sorry

end Silmaril
