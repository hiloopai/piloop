module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Decompilation
import Std.Data.String.ToNat

/-! # Exercise sheet 4, question 19 (a farboo decompiler)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 19.


Answer: `decompile`, for the code produced by `Decompilation.Stmt.compile`
(the sheet's patterns, with absolute labels). It pattern-matches the compiled
shapes, as the sheet's hints suggest:

* `MOV %j, v; ALU %(j−1), v′, %j; …; ALU %0, …` is an expression into `%0`;
* an expression followed by `MOV x, %0` is `x = e`;
* an expression followed by `CMPEQ %0, #0 → l` is an `if`: if the instruction
  before `l` is a `NOP` jumping forward past `l`, it is the jump over an `else`
  branch, otherwise there is no `else`;
* `MOV x, #lo; CMPGT x, #hi → l` with `x` not a temporary is a `for` loop
  whose body ends two instructions before `l`;
* a region of code is a statement followed by the rest of the region.

`decompile_compile`: decompiling compiled code gives back the program.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q19

open Decompilation

/-- Recover a value from an operand. -/
def ofOperand : Operand String → Val
  | .imm n => .num n
  | .var x => .var x

/-- Recover an operator. -/
def ofAlu : AluOp → Option Op
  | .add => some .add
  | .sub => some .sub
  | .mul => some .mul
  | _ => none

variable (code : List (Instr String ℕ))

/-- The `ALU` chain of an expression whose current temporary is `cur`, ending at
`%k`. -/
def exprTail (k : ℕ) : ℕ → FExpr → String → ℕ → Option (FExpr × ℕ)
  | 0, _, _, _ => none
  | fuel + 1, e, cur, pc =>
    if cur = temp k then some (e, pc) else
    match code[pc]? with
    | some (.alu op d a (.var c) _) =>
      if c = cur then
        match ofAlu op with
        | some o => exprTail k fuel (.op (ofOperand a) o e) d (pc + 1)
        | none => none
      else none
    | _ => none

/-- An expression computed into `%k` starting at `pc`, and the label after it. -/
def parseExpr (k pc : ℕ) : Option (FExpr × ℕ) :=
  match code[pc]? with
  | some (.alu2 .mov d a _) => exprTail code k (code.length + 1) (.val (ofOperand a)) d (pc + 1)
  | _ => none

mutual

/-- The statements of the region `[pc, stop)`. -/
def decRegion : ℕ → ℕ → ℕ → Option Stmt
  | 0, _, _ => none
  | fuel + 1, pc, stop =>
    match decOne fuel pc with
    | some (s, next) =>
      if next = stop then some s
      else if next < stop then (decRegion fuel next stop).map fun r => .seq s r
      else none
    | none => none

/-- One statement starting at `pc`, and the label after it. -/
def decOne : ℕ → ℕ → Option (Stmt × ℕ)
  | 0, _ => none
  | fuel + 1, pc =>
    match code[pc]?, code[pc + 1]? with
    | some (.alu2 .mov x (.imm lo) _), some (.cmp .gt (.var x') (.imm hi) stop _) =>
      if x = x' ∧ x.startsWith "%" = false then
        (decRegion fuel (pc + 2) (stop - 2)).map fun body => (.forLoop x lo hi body, stop)
      else none
    | _, _ =>
      match parseExpr code 0 pc with
      | some (e, pc') =>
        match code[pc']? with
        | some (.alu2 .mov x (.var t) _) =>
          if t = temp 0 then some (.assign x e, pc' + 1) else none
        | some (.cmp .eq (.var t) (.imm 0) tgt _) =>
          if t = temp 0 then
            match code[tgt - 1]? with
            | some (.nop fin) =>
              if tgt < fin then
                match decRegion fuel (pc' + 1) (tgt - 1), decRegion fuel tgt fin with
                | some st, some se => some (.ifElse e st se, fin)
                | _, _ => none
              else (decRegion fuel (pc' + 1) tgt).map fun body => (.ifThen e body, tgt)
            | _ => (decRegion fuel (pc' + 1) tgt).map fun body => (.ifThen e body, tgt)
          else none
        | _ => none
      | none => none

end

/-- The decompiler. -/
def decompile : Option Stmt := decRegion code (2 * code.length + 2) 0 code.length

/-! ## Correctness of the decompiler

The proof separates the structure of compiled code from the fuel: `At code pc xs`
says that `code` contains `xs` at label `pc`, and `dec_compile` shows, by
structural induction on the statement, that every fuel above twice the length
of the compiled code suffices. -/

/-- `code` contains `xs` starting at label `pc`. -/
def At (code : List (Instr String ℕ)) (pc : ℕ) (xs : List (Instr String ℕ)) : Prop :=
  ∃ pre post, code = pre ++ xs ++ post ∧ pre.length = pc

section At

variable {code : List (Instr String ℕ)} {pc q : ℕ} {xs ys : List (Instr String ℕ)}

end At

/-- The innermost value of an expression. -/
def leafOf : FExpr → Val
  | .val v => v
  | .op _ _ e => leafOf e

/-- The number of operators of an expression. -/
def depthOf : FExpr → ℕ
  | .val _ => 0
  | .op _ _ e => depthOf e + 1

/-- The non-loop branch of `decOne`: statements that start with an expression. -/
def decRest (fuel pc : ℕ) : Option (Stmt × ℕ) :=
  match parseExpr code 0 pc with
  | some (e, pc') =>
    match code[pc']? with
    | some (.alu2 .mov x (.var t) _) =>
      if t = temp 0 then some (.assign x e, pc' + 1) else none
    | some (.cmp .eq (.var t) (.imm 0) tgt _) =>
      if t = temp 0 then
        match code[tgt - 1]? with
        | some (.nop fin) =>
          if tgt < fin then
            match decRegion code fuel (pc' + 1) (tgt - 1), decRegion code fuel tgt fin with
            | some st, some se => some (.ifElse e st se, fin)
            | _, _ => none
          else (decRegion code fuel (pc' + 1) tgt).map fun body => (.ifThen e body, tgt)
        | _ => (decRegion code fuel (pc' + 1) tgt).map fun body => (.ifThen e body, tgt)
      else none
    | _ => none
  | none => none

/-- Statements that are not sequences. -/
def NonSeq : Stmt → Prop
  | .seq _ _ => False
  | _ => True

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q19
