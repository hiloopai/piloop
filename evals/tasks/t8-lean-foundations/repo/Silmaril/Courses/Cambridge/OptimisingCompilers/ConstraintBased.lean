module

public import Mathlib.Order.CompleteLattice.Basic
public import Mathlib.Logic.Relation
public import Mathlib.Data.Set.Lattice.Order

/-! # §13 Constraint-based analysis (notes pp. 25–26; slides pp. 99–104)

"In constraint-based analysis, the approach taken is that of walking the
program emitting constraints […] on the sets of values which variables or
expressions may take" (notes p. 25). The constraint systems of the course
(LVA, 0CFA, Andersen) are sets of inclusions whose solutions are closed under
arbitrary intersections (a *Moore family*), so the intersection of all
solutions is the least solution (`IsMoore.isLeast_sInf`, notes §13.1: "such sets
of such constraints have a minimal solution").

The notes' exercise (p. 26): the transitive closure `T` of a relation `R` is the
least solution of `R ⊆ T` and `{(x, y)} ⊆ T ∧ {(y, z)} ⊆ R ⇒ {(x, z)} ⊆ T`
(`transClosure_isLeast`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.ConstraintBased

variable {α : Type*} [CompleteLattice α]

/-- A constraint system, given by its set of solutions, is closed under
arbitrary meets. -/
def IsMoore (C : Set α) : Prop := ∀ S ⊆ C, sInf S ∈ C

/-- Membership in a pointwise meet of set-valued functions. -/
theorem mem_sInf_apply {ι γ : Type*} {S : Set (ι → Set γ)} {i : ι} {x : γ} :
    x ∈ sInf S i ↔ ∀ f ∈ S, x ∈ f i := sorry

/-- Inclusions valid in every member of `S` are valid in its meet. -/
theorem sInf_apply_subset {ι γ : Type*} {S : Set (ι → Set γ)} {i j : ι}
    (h : ∀ f ∈ S, f i ⊆ f j) : sInf S i ⊆ sInf S j := sorry

/-- The transitive-closure constraints of notes p. 26. -/
def TransClosureConstraints {β : Type*} (R : β → β → Prop) : Set (β → β → Prop) :=
  {T | (∀ x y, R x y → T x y) ∧ ∀ x y z, T x y → R y z → T x z}

/-- The transitive-closure constraints form a Moore family. -/
theorem transClosureConstraints_isMoore {β : Type*} (R : β → β → Prop) :
    IsMoore (TransClosureConstraints R) := sorry

/-- Their least solution is the transitive closure. -/
theorem transClosure_isLeast {β : Type*} (R : β → β → Prop) :
    IsLeast (TransClosureConstraints R) (Relation.TransGen R) := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.ConstraintBased
