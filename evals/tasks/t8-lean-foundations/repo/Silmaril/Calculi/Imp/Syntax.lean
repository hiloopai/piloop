module

public import Mathlib.Data.Int.Basic

/-! # Imp syntax

The while-language Imp of §1.1 of the notes. Arithmetic expressions are
built from integer literals and locations drawn from a parameter type `L`
with `+`, `-` and `*`. Boolean expressions compare arithmetic expressions
with `=`, `≤` and `<` and close under `¬` and `∧`. Programs are `skip`,
assignment, sequencing, conditionals and while loops. A state assigns an
integer to each location.
-/

@[expose] public section

namespace Silmaril.Calculi.Imp


/-- §1.1: arithmetic expressions `A ::= n | L | A + A | A - A | A * A`. -/
inductive Aexp (L : Type*) where
  /-- An integer literal. -/
  | num (n : ℤ)
  /-- The contents of a location. -/
  | loc (l : L)
  /-- Addition. -/
  | add (a₁ a₂ : Aexp L)
  /-- Subtraction. -/
  | sub (a₁ a₂ : Aexp L)
  /-- Multiplication. -/
  | mul (a₁ a₂ : Aexp L)
  deriving DecidableEq

/-- §1.1: boolean expressions
`B ::= true | false | A = A | A ≤ A | A < A | ¬B | B ∧ B`. -/
inductive Bexp (L : Type*) where
  /-- The constant `true`. -/
  | tt
  /-- The constant `false`. -/
  | ff
  /-- Equality test. -/
  | eq (a₁ a₂ : Aexp L)
  /-- Comparison `a₁ ≤ a₂`. -/
  | le (a₁ a₂ : Aexp L)
  /-- Strict comparison `a₁ < a₂`. -/
  | lt (a₁ a₂ : Aexp L)
  /-- Negation. -/
  | not (b : Bexp L)
  /-- Conjunction. -/
  | and (b₁ b₂ : Bexp L)
  deriving DecidableEq

/-- §1.1: programs
`C ::= skip | L := A | C; C | if B then C else C | while B do C`. -/
inductive Prog (L : Type*) where
  /-- Do nothing. -/
  | skip
  /-- Assignment `l := a`. -/
  | assign (l : L) (a : Aexp L)
  /-- Sequencing `c₁; c₂`. -/
  | seq (c₁ c₂ : Prog L)
  /-- The conditional `if b then c₁ else c₂`. -/
  | ite (b : Bexp L) (c₁ c₂ : Prog L)
  /-- The loop `while b do c`. -/
  | while (b : Bexp L) (c : Prog L)
  deriving DecidableEq

/-- §1.1: `State = L → ℤ`. -/
abbrev State (L : Type*) : Type _ := L → ℤ

end Silmaril.Calculi.Imp
