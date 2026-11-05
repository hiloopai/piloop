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

/-- Exercise 3 (and Example 11): `F_{b,c}` is continuous. Continuity follows
pointwise from Mathlib's continuity of `Part` bind
(`OmegaCompletePartialOrder.ContinuousHom.ωScottContinuous.bind`) and
of constants: at each state `s`, `F_{b,c}(w)(s)` is either the constant `s` or
`c(s) >>= w`, which is continuous in `w`. -/
theorem ωScottContinuous_whileFun : ωScottContinuous (whileFun b c) := sorry

/-- Exercise 3: `F_{b,c}` as a continuous map on `State ⇀ State`. -/
noncomputable def F : (State L → Part (State L)) →𝒄 (State L → Part (State L)) :=
  ContinuousHom.ofFun (whileFun b c) (ωScottContinuous_whileFun b c)

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

@[simp] theorem denote_skip (s : State L) : C⟦.skip⟧ s = Part.some s := sorry

@[simp] theorem denote_assign (l : L) (a : Aexp L) (s : State L) :
    C⟦.assign l a⟧ s = Part.some (Function.update s l (A⟦a⟧ s)) := sorry

@[simp] theorem denote_seq (c₁ c₂ : Prog L) (s : State L) :
    C⟦.seq c₁ c₂⟧ s = (C⟦c₁⟧ s).bind C⟦c₂⟧ := sorry

end Program

end Silmaril.Calculi.Imp
