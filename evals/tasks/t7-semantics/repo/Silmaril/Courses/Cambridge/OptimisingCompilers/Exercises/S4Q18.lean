module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Decompilation
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Execution
public import Mathlib.Data.Fin.VecNotation

/-! # Exercise sheet 4, question 18 (decompiling farboo)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 18.


The three fragments are `codeA`, `codeB`, `codeC`, transcribed into the course IR
with one node per instruction (`B lab` is a `NOP` to `lab`; the immediate `0` of
`MOV t01, 0` in (a) is `#0`). The answers:

(a) The blocks are `[0–4]`, `[5]`, `[6]`, `[7–8]`: a two-way branch on
`x + y = 0` joining at `label2`, which assigns `res1`. Since farboo's `if` tests
non-zero, the decompilation is `res1 = 0; if x + y then res1 = x end`
(`answerA`).

(b) `label1` heads a loop whose exit test compares the counter `t02`, from `1`,
with `10`, and whose body doubles `t01`: `res1 = 1; for i = 1 to 9 do
res1 = res1 * 2 end` (`answerB`), so `res1 = 512`.

(c) `t05 = x*x + y*y − 1`; if it is non-zero the result is `t05 + 1`, otherwise
`1`: `a = x * x; b = y * y; t = a + b; t = t - 1;
if t then res1 = t + 1 else res1 = 1 end` (`answerC`).

Each `…_correct` theorem says the fragment halts with the answer's `res1`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q18

open Decompilation

/-- Fragment (a). -/
def codeA : Flowgraph String (Fin 9) where
  entry := 0
  code := ![.alu2 .mov "t01" (.var "x") 1,
    .alu2 .mov "t02" (.var "y") 2,
    .alu .add "t03" (.var "t01") (.var "t02") 3,
    .alu2 .mov "t04" (.imm 0) 4,
    .cmp .eq (.var "t04") (.var "t03") 6 5,
    .nop 7,
    .alu2 .mov "t01" (.imm 0) 7,
    .alu2 .mov "res1" (.var "t01") 8,
    .exit ["res1"]]

/-- Fragment (b). -/
def codeB : Flowgraph String (Fin 9) where
  entry := 0
  code := ![.alu2 .mov "t01" (.imm 1) 1,
    .alu2 .mov "t02" (.imm 1) 2,
    .alu2 .mov "t03" (.imm 10) 3,
    .cmp .eq (.var "t02") (.var "t03") 7 4,
    .alu .mul "t01" (.var "t01") (.imm 2) 5,
    .alu .add "t02" (.var "t02") (.imm 1) 6,
    .nop 3,
    .alu2 .mov "res1" (.var "t01") 8,
    .exit ["res1"]]

/-- Fragment (c). -/
def codeC : Flowgraph String (Fin 15) where
  entry := 0
  code := ![.alu2 .mov "t01" (.var "x") 1,
    .alu2 .mov "t02" (.var "y") 2,
    .alu .mul "t03" (.var "t01") (.var "t01") 3,
    .alu .mul "t04" (.var "t02") (.var "t02") 4,
    .alu .add "t05" (.var "t03") (.var "t04") 5,
    .alu .sub "t05" (.var "t05") (.imm 1) 6,
    .alu2 .mov "t06" (.imm 0) 7,
    .cmp .ne (.var "t05") (.var "t06") 10 8,
    .alu2 .mov "t07" (.imm 1) 9,
    .nop 13,
    .alu .add "t05" (.var "t05") (.imm 1) 11,
    .alu2 .mov "t07" (.var "t05") 12,
    .nop 13,
    .alu2 .mov "res1" (.var "t07") 14,
    .exit ["res1"]]

/-- (a) `res1 = 0; if x + y then res1 = x end`. -/
def answerA : Stmt :=
  .seq (.assign "res1" (.val (.num 0)))
    (.ifThen (.op (.var "x") .add (.val (.var "y"))) (.assign "res1" (.val (.var "x"))))

/-- (b) `res1 = 1; for i = 1 to 9 do res1 = res1 * 2 end`. -/
def answerB : Stmt :=
  .seq (.assign "res1" (.val (.num 1)))
    (.forLoop "i" 1 9 (.assign "res1" (.op (.var "res1") .mul (.val (.num 2)))))

/-- (c) `a = x * x; b = y * y; t = a + b; t = t - 1;
if t then res1 = t + 1 else res1 = 1 end`. -/
def answerC : Stmt :=
  .seq (.assign "a" (.op (.var "x") .mul (.val (.var "x"))))
    (.seq (.assign "b" (.op (.var "y") .mul (.val (.var "y"))))
      (.seq (.assign "t" (.op (.var "a") .add (.val (.var "b"))))
        (.seq (.assign "t" (.op (.var "t") .sub (.val (.num 1))))
          (.ifElse (.val (.var "t")) (.assign "res1" (.op (.var "t") .add (.val (.num 1))))
            (.assign "res1" (.val (.num 1)))))))

/-- A fragment halts with the answer's value of `res1`. -/
def Decompiles {n : ℕ} (G : Flowgraph String (Fin n)) (s : Stmt) : Prop :=
  ∀ (env : Env String) (mem : Mem), ∃ t : Config String (Fin n),
    G.Steps ⟨G.entry, env, mem⟩ [] t ∧ G.Halted t ∧ G.resultValues t = [s.run env "res1"]

/-- Evaluate the result of a run and the answer's value of `res1`. -/
syntax "res_simp" : tactic

macro_rules
  | `(tactic| res_simp) => `(tactic| simp_all [Flowgraph.resultValues, Function.update_apply,
      answerA, answerB, answerC, Stmt.run, FExpr.eval, Val.eval, Op.eval, UnOp.eval, AluOp.eval,
      Operand.eval])

theorem answerA_correct : Decompiles codeA answerA := sorry

theorem answerB_correct : Decompiles codeB answerB := sorry

theorem answerC_correct : Decompiles codeC answerC := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q18
