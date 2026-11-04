module

public import Mathlib.Order.Basic

@[expose] public section

/-! # Semantic contracts for program transformations

A `Contract X` fixes an invariant on programs and a preorder of acceptable
replacements. `Rel source replacement` always places the original program first.
A verified pass may assume the invariant and must preserve it.

The relation is a parameter: this API does not choose a language, an execution
semantics, an undefined-behavior policy, or a notion of observation. An instance
must justify its chosen relation's reflexivity and transitivity.

`Contract.pullback` obtains a contract from a semantic preorder and a denotation.
`Contract.Le` compares replacement relations on equivalent invariants; it does not
weaken or strengthen the admitted input invariant. Contract inclusion induces
`Pass.weaken`, which preserves the executable transformation.
-/

namespace Silmaril.Compile.PassAPI

universe u v

/-- A semantic contract on programs of type `X`: a preorder of acceptable replacements
and an invariant. -/
structure Contract (X : Type u) where
  /-- `Rel x y`: `y` may replace `x`. -/
  Rel : X → X → Prop
  refl : ∀ x, Rel x x
  trans : ∀ {a b c : X}, Rel a b → Rel b c → Rel a c
  /-- The invariant passes assume and preserve. -/
  Inv : X → Prop

namespace Contract

variable {X : Type u}

/-- Pull a preorder on a semantic carrier `S` back along a denotation, with an
invariant on programs. -/
def pullback {S : Type v} (R : S → S → Prop) (hrefl : ∀ s, R s s)
    (htrans : ∀ {a b c : S}, R a b → R b c → R a c) (den : X → S) (Inv : X → Prop) :
    Contract X where
  Rel x y := R (den x) (den y)
  refl x := hrefl (den x)
  trans h₁ h₂ := htrans h₁ h₂
  Inv := Inv

@[simp] theorem pullback_rel {S : Type v} (R : S → S → Prop) (hrefl : ∀ s, R s s)
    (htrans : ∀ {a b c : S}, R a b → R b c → R a c) (den : X → S) (Inv : X → Prop)
    (x y : X) : (pullback R hrefl htrans den Inv).Rel x y = R (den x) (den y) := rfl

/-- Contract inclusion: the invariants are equivalent, and every `C`-replacement
of an invariant-satisfying program is a `D`-replacement. -/
structure Le (C D : Contract X) : Prop where
  inv_iff : ∀ x, C.Inv x ↔ D.Inv x
  rel : ∀ x y, C.Inv x → C.Rel x y → D.Rel x y

theorem le_refl' (C : Contract X) : Le C C := ⟨fun _ => Iff.rfl, fun _ _ _ h => h⟩

theorem le_trans' {C D E : Contract X} (h₁ : Le C D) (h₂ : Le D E) : Le C E :=
  ⟨fun x => (h₁.inv_iff x).trans (h₂.inv_iff x),
   fun x y hx h => h₂.rel x y ((h₁.inv_iff x).mp hx) (h₁.rel x y hx h)⟩

/-- Contracts on `X` form a preorder under inclusion. -/
instance : Preorder (Contract X) where
  le := Le
  le_refl := le_refl'
  le_trans _ _ _ := le_trans'

end Contract

end Silmaril.Compile.PassAPI
