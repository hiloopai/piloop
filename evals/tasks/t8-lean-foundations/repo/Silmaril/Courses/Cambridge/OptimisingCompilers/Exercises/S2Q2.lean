module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Strictness

/-! # Exercise sheet 2, question 2 (strictness)

Source: `cam-optcomp-2025-exercise-sheet-2` in `references.bib`, question 2.


(a) `Prog.IsStrictIn g 1` (notes p. 22): `g(d₀, ⊥, d₂) = ⊥` for all `d₀, d₂`;
operationally, whenever the second argument loops, so does the call, so it is
safe to evaluate that argument first (call-by-value).

(b) `fSharp_eq`: `f♯ = λ(x, y).0` and `gSharp_eq`: `g♯ = λ(x, y).x ∧ y`. Both
are strict in both parameters (`f_strict`, `g_strict`), so both allow
call-by-value for both; but `f` never evaluates its parameters: it is strict
because it never terminates, not because it needs them.

(c) `fcSharp_eq`: `f♯(x, y, z) = x ∧ (y ∨ (y ∧ z)) = x ∧ y`.

(d) The bit vector of `f1(x, y, z) = if x then y else z` is `(1, 0, 0)`
(`f1_bits`): it is strict in `x` only. From bits alone, `g1(x, y) =
f1(x, y, y+1)` can only be found strict in `x`, since `y` reaches `f1` only in
non-strict positions. The strictness function `f1♯ = x ∧ (y ∨ z)` shows that
`g1` is strict in `y` too (`g1_strict_y`), so `y` can be passed by value.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S2Q2

open Strictness

/-- Built-ins: `cond`, `=`, `+`, `0` and `+1`. -/
inductive P where
  | cond | eq | plus | zero | add1

@[reducible] def P.arity : P → ℕ
  | .cond => 3
  | .eq | .plus => 2
  | .zero => 0
  | .add1 => 1

def P.sem : (a : P) → (Fin a.arity → Part ℤ) → Part ℤ
  | .cond, d => (d 0).bind fun p => if p = 0 then d 2 else d 1
  | .eq, d => (d 0).bind fun a => (d 1).map fun b => if a = b then 1 else 0
  | .plus, d => (d 0).bind fun a => (d 1).map fun b => a + b
  | .zero, _ => Part.some 0
  | .add1, d => (d 0).map (· + 1)

def P.sharp : (a : P) → (Fin a.arity → Bool) → Bool
  | .cond, x => x 0 && (x 1 || x 2)
  | .eq, x | .plus, x => x 0 && x 1
  | .zero, _ => true
  | .add1, x => x 0

/-- Signatures with `n` user functions of arities `far`. -/
@[reducible] def sig (n : ℕ) (far : Fin n → ℕ) : Sig where
  B := P
  bar := P.arity
  sem := P.sem
  sharp := P.sharp
  n := n
  far := far

/-! ## (b) -/

/-- `f(x, y) = f(x, y)` (function 0) and `g(x, y) = x + y` (function 1). -/
def progB : Prog (sig 2 fun _ => 2) where
  body
    | ⟨0, _⟩ => .call 0 ![.arg 0, .arg 1]
    | ⟨1, _⟩ => .prim P.plus ![.arg 0, .arg 1]
    | ⟨n + 2, h⟩ => absurd h (by simp)

theorem f_strict (i : Fin 2) : Prog.IsStrictIn (progB.denote 0) i := sorry

theorem g_strict (i : Fin 2) : Prog.IsStrictIn (progB.denote 1) i := sorry

/-! ## (c) -/

/-- `f(x, y, z) = if x = 0 then y else y + z`, as
`cond(eq(x, 0), y, plus(y, z))`: `eq` yields `1` when its arguments are equal,
and `cond` takes its second argument on a non-zero test. -/
def progC : Prog (sig 1 fun _ => 3) where
  body _ := .prim P.cond ![.prim P.eq ![.arg 0, .prim P.zero ![]], .arg 1,
    .prim P.plus ![.arg 1, .arg 2]]

theorem fcSharp_eq : progC.sharp 0 = fun x => x 0 && x 1 := sorry

/-! ## (d) -/

/-- `f1(x, y, z) = if x then y else z` (function 0, arity 3) and
`g1(x, y) = f1(x, y, y+1)` (function 1, arity 2). -/
@[reducible] def sigD : Sig := sig 2 ![3, 2]

/-- The body of `f1`. -/
def f1Body : Expr sigD 3 := .prim P.cond ![.arg 0, .arg 1, .arg 2]

/-- The body of `g1`. -/
def g1Body : Expr sigD 2 := .call 0 ![.arg 0, .arg 1, .prim P.add1 ![.arg 1]]

def progD : Prog sigD where
  body := Fin.cases (motive := fun f => Expr sigD (sigD.far f)) f1Body
    (Fin.cases (motive := fun i => Expr sigD (sigD.far i.succ)) g1Body fun j => j.elim0)

/-- The bit vector of `f1`: strict in `x` only. -/
theorem f1_bits (i : Fin 3) : Prog.IsStrictIn (progD.denote 0) i ↔ i.val = 0 := sorry

/-- The strictness function shows `g1` strict in `y`. -/
theorem g1_strict_y : progD.sharp 1 (fun j => decide (j.val ≠ 1)) = false := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S2Q2
