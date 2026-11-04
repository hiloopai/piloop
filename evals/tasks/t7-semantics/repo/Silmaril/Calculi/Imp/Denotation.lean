module

public import Silmaril.Semantics.Domain.Examples
public import Silmaril.Calculi.Imp.Syntax

/-! # Denotational semantics of Imp

The semantic functions of §1.1–1.2 of the notes:

* `A⟦a⟧ : State → ℤ` and `B⟦b⟧ : State → Bool` interpret expressions;
* `C⟦c⟧ : State → Part State` interprets programs as elements of the domain of
  partial functions `State ⇀ State` (Examples 1 and 4).

Sequencing is Kleisli composition in `Part` and a while loop denotes the least
fixed point of `F b c : w ↦ λs. if b(s) then (w ∘ c)(s) else s`
(Example 11, Exercise 3 and Example 13). The loop satisfies the unfolding
equation of §1.2, `C⟦while B do C⟧ = C⟦if B then (C; while B do C) else skip⟧`.

The notes' Exercise 3 allows `b` to be partial. Boolean expressions here
always terminate, so `b` is total, and `F b c` is strict exactly when `b`
holds everywhere.
-/

@[expose] public section

namespace Silmaril.Calculi.Imp


open OmegaCompletePartialOrder Silmaril.Semantics.Domain

variable {L : Type*}

/-- §1.1: the semantics of arithmetic expressions. -/
def Aexp.denote : Aexp L → State L → ℤ
  | .num n, _ => n
  | .loc l, s => s l
  | .add a₁ a₂, s => a₁.denote s + a₂.denote s
  | .sub a₁ a₂, s => a₁.denote s - a₂.denote s
  | .mul a₁ a₂, s => a₁.denote s * a₂.denote s

/-- §1.1: the semantics of boolean expressions. -/
def Bexp.denote : Bexp L → State L → Bool
  | .tt, _ => true
  | .ff, _ => false
  | .eq a₁ a₂, s => decide (a₁.denote s = a₂.denote s)
  | .le a₁ a₂, s => decide (a₁.denote s ≤ a₂.denote s)
  | .lt a₁ a₂, s => decide (a₁.denote s < a₂.denote s)
  | .not b, s => !b.denote s
  | .and b₁ b₂, s => b₁.denote s && b₂.denote s

@[inherit_doc] scoped notation "A⟦" a "⟧" => Aexp.denote a
@[inherit_doc] scoped notation "B⟦" b "⟧" => Bexp.denote b

section WhileFunctional

variable (b : State L → Bool) (c : State L → Part (State L))

/-- §1.2: the functional `F_{b,c}(w) = λs. if b(s) then (w ∘ c)(s) else s`,
where `w ∘ c` is composition of partial functions. -/
def whileFun (w : State L → Part (State L)) : State L → Part (State L) :=
  fun s => if b s then (c s).bind w else Part.some s

/-- Exercise 3: `F_{b,c}` is monotone. -/
theorem whileFun_monotone : Monotone (whileFun b c) := by
  intro w w' h s t ht
  unfold whileFun at ht ⊢
  split at ht
  · obtain ⟨u, hu, ht⟩ := Part.mem_bind_iff.1 ht
    simp only [*]
    exact Part.mem_bind_iff.2 ⟨u, hu, h u t ht⟩
  · simp only [*]
    exact ht

/-- Exercise 3 (and Example 11): `F_{b,c}` is continuous. Continuity follows
pointwise from Mathlib's continuity of `Part` bind
(`OmegaCompletePartialOrder.ContinuousHom.ωScottContinuous.bind`) and
of constants: at each state `s`, `F_{b,c}(w)(s)` is either the constant `s` or
`c(s) >>= w`, which is continuous in `w`. -/
theorem ωScottContinuous_whileFun : ωScottContinuous (whileFun b c) :=
  .of_apply₂ fun s => by
    unfold whileFun
    cases b s
    · exact OmegaCompletePartialOrder.ωScottContinuous.const
    · simp only [ite_true, ← Part.bind_eq_bind]
      exact OmegaCompletePartialOrder.ContinuousHom.ωScottContinuous.bind
        OmegaCompletePartialOrder.ωScottContinuous.const
        OmegaCompletePartialOrder.ωScottContinuous.id

/-- Exercise 3: `F_{b,c}` as a continuous map on `State ⇀ State`. -/
noncomputable def F : (State L → Part (State L)) →𝒄 (State L → Part (State L)) :=
  ContinuousHom.ofFun (whileFun b c) (ωScottContinuous_whileFun b c)

theorem F_apply (w : State L → Part (State L)) (s : State L) :
    F b c w s = if b s then (c s).bind w else Part.some s := rfl

/-- Exercise 3: `F_{b,c}` is strict exactly when the guard `b` always holds. -/
theorem strict_F_iff : Strict (F b c) ↔ ∀ s, b s = true := by
  constructor
  · intro h s
    by_contra hb
    have := congrFun h s
    simp only [F_apply, hb, partialFun_bot_apply] at this
    exact Part.some_ne_none s this
  · intro h
    funext s
    simp only [F_apply, h s, partialFun_bot_apply, ite_true]
    exact Part.eq_none_iff.2 fun t ht => by
      obtain ⟨_, -, ht⟩ := Part.mem_bind_iff.1 ht
      exact Part.notMem_none t ht

end WhileFunctional

/-- §1.2 and Example 13: the semantics of programs as partial functions on
states. A while loop denotes `fix F_{B⟦b⟧, C⟦c⟧}`. -/
noncomputable def Prog.denote [DecidableEq L] : Prog L → State L → Part (State L)
  | .skip => fun s => Part.some s
  | .assign l a => fun s => Part.some (Function.update s l (A⟦a⟧ s))
  | .seq c₁ c₂ => fun s => (c₁.denote s).bind c₂.denote
  | .ite b c₁ c₂ => fun s => if B⟦b⟧ s then c₁.denote s else c₂.denote s
  | .while b c => fix (F B⟦b⟧ c.denote)

@[inherit_doc] scoped notation "C⟦" c "⟧" => Prog.denote c

section Program

variable [DecidableEq L]

@[simp] theorem denote_skip (s : State L) : C⟦.skip⟧ s = Part.some s := rfl

@[simp] theorem denote_assign (l : L) (a : Aexp L) (s : State L) :
    C⟦.assign l a⟧ s = Part.some (Function.update s l (A⟦a⟧ s)) := rfl

@[simp] theorem denote_seq (c₁ c₂ : Prog L) (s : State L) :
    C⟦.seq c₁ c₂⟧ s = (C⟦c₁⟧ s).bind C⟦c₂⟧ := rfl

@[simp] theorem denote_ite (b : Bexp L) (c₁ c₂ : Prog L) (s : State L) :
    C⟦.ite b c₁ c₂⟧ s = if B⟦b⟧ s then C⟦c₁⟧ s else C⟦c₂⟧ s := rfl

theorem denote_while (b : Bexp L) (c : Prog L) :
    C⟦.while b c⟧ = fix (F B⟦b⟧ C⟦c⟧) := rfl

/-- §1.2: a while loop is a fixed point of `F_{B⟦b⟧, C⟦c⟧}`. -/
theorem F_denote_while (b : Bexp L) (c : Prog L) :
    F B⟦b⟧ C⟦c⟧ C⟦.while b c⟧ = C⟦.while b c⟧ :=
  fix_eq _

/-- §1.2: the denotation of `while B do C` is the least fixed point of
`F_{B⟦b⟧, C⟦c⟧}` (Theorem 6 applied as in Example 13). -/
theorem isLeast_denote_while (b : Bexp L) (c : Prog L) :
    IsLeast (Function.fixedPoints (F B⟦b⟧ C⟦c⟧)) C⟦.while b c⟧ :=
  isLeast_fixedPoints_fix _

/-- §1.2: loop unfolding,
`⟦while B do C⟧ = ⟦if B then (C; while B do C) else skip⟧`. -/
theorem denote_while_unfold (b : Bexp L) (c : Prog L) :
    C⟦.while b c⟧ = C⟦.ite b (.seq c (.while b c)) .skip⟧ := by
  conv_lhs => rw [← F_denote_while]
  rfl

end Program

end Silmaril.Calculi.Imp
