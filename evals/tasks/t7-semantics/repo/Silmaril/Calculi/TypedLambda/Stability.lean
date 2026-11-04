module

public import Silmaril.Calculi.TypedLambda.LogicalRelation
public import Silmaril.Semantics.Domain.Stable

/-! # The stability logical relation

Berry's stability (`Silmaril.Semantics.Domain.Stable`) as a logical relation over any
domain model. At every base type we take the ternary meet relation
`Silmaril.Semantics.Domain.MeetRel`: `d₀` and `d₁` have a common upper bound and `d₂`
is their greatest lower bound. The logical relation `MeetLogRel M` extends it
to all types. By the fundamental lemma (`LogRel.fundamental`), every term of
a signature whose operators preserve the relation denotes a family of
elements that preserve it; at a first-order type such as
`bool -> bool -> bool` over flat base domains, this says the denotation is a
stable function (`Silmaril.Calculi.PCF.stable_denoteClosed_bool₂`).

As with Sieber's relation (`Silmaril.Calculi.TypedLambda.SieberLogRel`), the triples
`(⊥, x, ⊥)` and `(x, ⊥, ⊥)` are related at every type (`of_bot_left`,
`of_bot_right`), which is what makes the conditional, into any type, preserve
the relation (`condFun_mem`). Admissibility (needed for `fix`) is checked at
base types; on flat domains it holds by `Silmaril.Semantics.Domain.Flat.meetRel_admissible`.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda


open OmegaCompletePartialOrder Silmaril.Semantics.Domain Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {S : Signature} {M : Model S}

/-- The stability logical relation over a model: the meet relation `MeetRel`
at every base type, extended to all types. -/
abbrev MeetLogRel (M : Model S) : (τ : Ty S.Base) → (Fin 3 → M.ty τ) → Prop :=
  LogRel M fun _ d => MeetRel d

namespace MeetLogRel

/-- Triples `(⊥, x, ⊥)` are related at every type. -/
theorem of_bot_left {τ : Ty S.Base} {d : Fin 3 → M.ty τ} (h₀ : d 0 = ⊥) (h₂ : d 2 = ⊥) :
    MeetLogRel M τ d :=
  LogRel.of_forall_eq_bot (fun j => j = 0 ∨ j = 2)
    (fun _ _ h => MeetRel.of_bot_left (h 0 (Or.inl rfl)) (h 2 (Or.inr rfl))) τ d
    fun j hj => by rcases hj with rfl | rfl; exacts [h₀, h₂]

/-- Triples `(x, ⊥, ⊥)` are related at every type. -/
theorem of_bot_right {τ : Ty S.Base} {d : Fin 3 → M.ty τ} (h₁ : d 1 = ⊥) (h₂ : d 2 = ⊥) :
    MeetLogRel M τ d :=
  LogRel.of_forall_eq_bot (fun j => j = 1 ∨ j = 2)
    (fun _ _ h => MeetRel.of_bot_right (h 1 (Or.inl rfl)) (h 2 (Or.inr rfl))) τ d
    fun j hj => by rcases hj with rfl | rfl; exacts [h₁, h₂]

/-- The conditional `if c then x else y`, with a related triple of conditions
in `𝔹⊥`, preserves the stability relation at every type. -/
theorem condFun_mem {τ : Ty S.Base} {c : Fin 3 → Part Bool} (hc : MeetRel c)
    {x y : Fin 3 → M.ty τ} (hx : MeetLogRel M τ x) (hy : MeetLogRel M τ y) :
    MeetLogRel M τ fun i => condFun (c i) (x i) (y i) := by
  rcases Flat.meetRel_iff.1 hc with ⟨h₀, h₂⟩ | ⟨h₁, h₂⟩ | ⟨h₀, h₂⟩
  · exact of_bot_left (by simp only [h₀]; exact condFun_none _ _)
      (by simp only [h₂]; exact condFun_none _ _)
  · exact of_bot_right (by simp only [h₁]; exact condFun_none _ _)
      (by simp only [h₂]; exact condFun_none _ _)
  · have hc' : ∀ i : Fin 3, c i = c 0 := fun i => by
      match i with
      | 0 => rfl
      | 1 => exact h₀.symm
      | 2 => exact (h₀.trans h₂).symm
    simp only [hc']
    rcases condFun_cases (c 0) with h | h | h
    · rw [h]
      exact of_bot_left (condFun_none _ _) (condFun_none _ _)
    · simpa only [h, condFun_true] using hx
    · simpa only [h, condFun_false] using hy

end MeetLogRel

end Silmaril.Calculi.TypedLambda
