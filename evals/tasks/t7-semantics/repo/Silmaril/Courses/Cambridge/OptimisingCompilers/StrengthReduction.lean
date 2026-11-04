module

public import Mathlib.Algebra.Ring.Int.Defs
public import Mathlib.Logic.Function.Basic
public import Mathlib.Logic.Function.Iterate
public import Mathlib.Tactic.Ring

/-! # §10 Algebraic identities and strength reduction (notes pp. 18–19;
slides pp. 84–88)

*Algebraic identities* (notes p. 18) are tree-to-tree rewrites such as
`e + 0 → e` and `(e + n) + m → e + (n + m)`, valid without analysis
(`simplify`, `eval_simplify`).

*Strength reduction* (notes pp. 18–19): an *induction variable* `i` is updated
in the loop only by `i := i + c`; a variable `j` whose only assignment is
`j := c₂ + c₁ * i` (before the update of `i`) can instead be initialised to
`c₂ + c₁ * i` on entry and incremented by `c₁ * c` at the end of each
iteration. `Loop` is the loop shape of the notes' explanation, over a body
that does not touch `i` or `j`; `strengthReduce_correct` says the reduced loop
computes the same values of every variable.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.StrengthReduction

/-! ## Algebraic identities -/

/-- Integer expression trees. -/
inductive Expr (V : Type*) where
  | var (x : V)
  | lit (n : ℤ)
  | add (a b : Expr V)
  | mul (a b : Expr V)

namespace Expr

variable {V : Type*}

/-- Evaluation. -/
def eval (env : V → ℤ) : Expr V → ℤ
  | .var x => env x
  | .lit n => n
  | .add a b => a.eval env + b.eval env
  | .mul a b => a.eval env * b.eval env

/-- One bottom-up pass of `e + 0 → e`, `0 + e → e`, `e * 1 → e`,
`(e + n) + m → e + (n + m)`, and `2 * e → e + e` (notes p. 18). -/
def simplify : Expr V → Expr V
  | .add a b => addS (simplify a) (simplify b)
  | .mul a b => mulS (simplify a) (simplify b)
  | e => e
where
  /-- Is the expression the literal `n`? -/
  isLit (n : ℤ) : Expr V → Bool
    | .lit m => m == n
    | _ => false
  /-- Smart `+`. -/
  addS (a b : Expr V) : Expr V :=
    if isLit 0 b then a
    else if isLit 0 a then b
    else match a, b with
      | .add e (.lit n), .lit m => .add e (.lit (n + m))
      | _, _ => .add a b
  /-- Smart `*`. -/
  mulS (a b : Expr V) : Expr V :=
    if isLit 1 b then a
    else if isLit 2 a then .add b b
    else .mul a b

/-- The identities hold universally over the integers. -/
theorem eval_simplify (env : V → ℤ) (e : Expr V) : e.simplify.eval env = e.eval env := sorry

end Expr

/-! ## Strength reduction in loops -/

/-- A counted loop: `i := i₀; while i < bound do { j := c₂ + c₁ * i; body; i := i + c }`.
The body is any update of the other variables that may read `j` and `i`. -/
structure Loop (V : Type*) where
  i : V
  j : V
  c : ℤ
  c₁ : ℤ
  c₂ : ℤ
  body : (V → ℤ) → (V → ℤ)

namespace Loop

variable {V : Type*} [DecidableEq V] (L : Loop V)

/-- The body does not assign `i` or `j`. -/
def BodyPreserves : Prop := ∀ env, L.body env L.i = env L.i ∧ L.body env L.j = env L.j

/-- One iteration of the original loop. -/
def iter (env : V → ℤ) : V → ℤ :=
  let env₁ := Function.update env L.j (L.c₂ + L.c₁ * env L.i)
  let env₂ := L.body env₁
  Function.update env₂ L.i (env₂ L.i + L.c)

/-- One iteration of the reduced loop: no multiplication, `j` is incremented by
`c₁ * c` (a constant). -/
def iterReduced (env : V → ℤ) : V → ℤ :=
  let env₂ := L.body env
  Function.update (Function.update env₂ L.i (env₂ L.i + L.c)) L.j (env₂ L.j + L.c₁ * L.c)

/-- Entry code of the reduced loop: `j := c₂ + c₁ * i`. -/
def enter (env : V → ℤ) : V → ℤ := Function.update env L.j (L.c₂ + L.c₁ * env L.i)

/-- Strength reduction is correct: after `n` iterations, the reduced loop agrees
with the original on every variable except `j`, where it holds the value the
original will assign at the start of the next iteration, `c₂ + c₁ * i`. -/
theorem strengthReduce_correct (hij : L.i ≠ L.j) (hbody : L.BodyPreserves) (n : ℕ)
    (env : V → ℤ) :
    let orig := L.iter^[n] env
    let red := L.iterReduced^[n] (L.enter env)
    (∀ x, x ≠ L.j → red x = orig x) ∧ red L.j = L.c₂ + L.c₁ * orig L.i := sorry

end Loop

end Silmaril.Courses.Cambridge.OptimisingCompilers.StrengthReduction
