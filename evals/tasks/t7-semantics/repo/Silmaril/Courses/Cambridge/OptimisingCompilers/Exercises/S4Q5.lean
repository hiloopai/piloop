module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q4

/-! # Exercise sheet 4, question 5 (optimising with matrix sizes)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 5.


Answer: re-associating a chain of products. Multiplying an `m × n` by an `n × p`
matrix costs `m·n·p` scalar multiplications, so the bracketing of a chain
matters, and the sizes inferred by `S4Q4.Derives` determine the cheapest one
(the matrix-chain ordering problem). `cost` computes the cost and size of an
expression; for `A : 10 × 100`, `B : 100 × 5`, `C : 5 × 50`, `(A × B) × C` costs
`7500` and `A × (B × C)` costs `75000` (`chain_example`), and the two have the
same value (`reassoc_eval`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q5

open S4Q4

/-- The number of scalar multiplications to evaluate an expression, with its
size, or `none` on a size mismatch. -/
def cost (Γ : Ctx) : MExpr → Option (ℕ × (ℕ × ℕ))
  | .var A => (Γ A).map fun s => (0, s)
  | .add a b | .sub a b => do
    let (c, s) ← cost Γ a
    let (c', s') ← cost Γ b
    if s = s' then pure (c + c', s) else none
  | .mul a b => do
    let (c, (m, n)) ← cost Γ a
    let (c', (n', p)) ← cost Γ b
    if n = n' then pure (c + c' + m * n * p, (m, p)) else none
  | .transpose a => do
    let (c, (m, n)) ← cost Γ a
    pure (c, (n, m))

/-- `A : 10 × 100, B : 100 × 5, C : 5 × 50`. -/
def Γ₁ : Ctx := fun x =>
  if x = "A" then some (10, 100) else if x = "B" then some (100, 5)
  else if x = "C" then some (5, 50) else none

/-- The bracketing matters: `7500` against `75000`. -/
theorem chain_example :
    cost Γ₁ (.mul (.mul (.var "A") (.var "B")) (.var "C")) = some (7500, (10, 50)) ∧
      cost Γ₁ (.mul (.var "A") (.mul (.var "B") (.var "C"))) = some (75000, (10, 50)) := sorry

/-- Re-association preserves the value. -/
theorem reassoc_eval (ρ : String → Option SMat) (a b c : MExpr) :
    eval ρ (.mul (.mul a b) c) = eval ρ (.mul a (.mul b c)) := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q5
