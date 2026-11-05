module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
import Std.Data.String.ToNat

/-! # Part D: Decompilation and reverse engineering (notes pp. 39–41;
slides pp. 127–131)

"Extracting the flowgraph from an assembler program is easy. The trick is then
to match intervals of the flowgraph with higher-level control structures, e.g.
loops, if-then-else" (notes p. 40).

The development uses the structured language *farboo* of exercise sheet 4
(questions 18–19):

```
statements ::= statement statements | statement
statement  ::= for var = N to N do statements end
             | if expr then statements end
             | if expr then statements else statements end
             | var = expr
expr       ::= val operator expr | val
```

* `Stmt.run` is its semantics (`if` tests non-zero, as in C);
* `compile` translates it to the course's 3-address code, with absolute labels,
  computing expressions into temporaries `%0, %1, …` as in the sheet's code;
* `compile_correct`: the compiled flowgraph halts with the program's final
  values in every non-temporary variable;
* `compile_injective`: compilation loses no structure, so decompilation of
  compiled code is well defined. Exercise S4Q19 gives the decompiler.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Decompilation

/-- `val ::= N | var`. -/
inductive Val where
  | num (n : ℤ)
  | var (x : String)
  deriving DecidableEq

/-- `operator ::= + | − | ∗ | \` (the last read as integer division). -/
inductive Op where
  | add | sub | mul | div
  deriving DecidableEq

/-- `expr ::= val operator expr | val`. -/
inductive FExpr where
  | val (v : Val)
  | op (v : Val) (o : Op) (e : FExpr)
  deriving DecidableEq

/-- Statements; `seq` is `statement statements`. -/
inductive Stmt where
  | assign (x : String) (e : FExpr)
  | forLoop (x : String) (lo hi : ℤ) (body : Stmt)
  | ifThen (c : FExpr) (body : Stmt)
  | ifElse (c : FExpr) (t e : Stmt)
  | seq (s₁ s₂ : Stmt)
  deriving DecidableEq

abbrev FEnv := String → ℤ

def Val.eval (env : FEnv) : Val → ℤ
  | .num n => n
  | .var x => env x

def Op.eval : Op → ℤ → ℤ → ℤ
  | .add, a, b => a + b
  | .sub, a, b => a - b
  | .mul, a, b => a * b
  | .div, a, b => a / b

def FExpr.eval (env : FEnv) : FExpr → ℤ
  | .val v => v.eval env
  | .op v o e => o.eval (v.eval env) (e.eval env)

/-- Semantics. `for x = lo to hi` runs the body for `x = lo, …, hi` and leaves
`x` one past the last value, as the C-style compiled loop does (the body may
not assign `x`, see `Stmt.Supported`). -/
def Stmt.run : Stmt → FEnv → FEnv
  | .assign x e, env => Function.update env x (e.eval env)
  | .forLoop x lo hi body, env =>
    let env' := (List.range (hi - lo + 1).toNat).foldl
      (fun env k => body.run (Function.update env x (lo + k))) env
    Function.update env' x (max lo (hi + 1))
  | .ifThen c body, env => if c.eval env ≠ 0 then body.run env else env
  | .ifElse c t e, env => if c.eval env ≠ 0 then t.run env else e.run env
  | .seq s₁ s₂, env => s₂.run (s₁.run env)

/-! ## Compilation to 3-address code -/

/-- Temporaries `%k`. -/
def temp (k : ℕ) : String := "%" ++ toString k

/-- Program variables never start with `%`. -/
def IsTemp (x : String) : Prop := x.startsWith "%" = true

def Val.toOperand : Val → Operand String
  | .num n => .imm n
  | .var x => .var x

def Op.toAlu : Op → Option AluOp
  | .add => some .add
  | .sub => some .sub
  | .mul => some .mul
  | .div => none

/-- Compile an expression into temporary `%k` (using `%k+1, …` for
subexpressions), starting at label `pc`. The IR has no division, so `Op.div`
is excluded by `FExpr.Supported`. -/
def FExpr.compile (k pc : ℕ) : FExpr → List (Instr String ℕ)
  | .val v => [.alu2 .mov (temp k) v.toOperand (pc + 1)]
  | .op v o e =>
    let ce := e.compile (k + 1) pc
    ce ++ [.alu ((o.toAlu).getD .add) (temp k) v.toOperand (.var (temp (k + 1))) (pc + ce.length + 1)]

/-- Values mention no temporaries. -/
def Val.Supported : Val → Prop
  | .num _ => True
  | .var x => ¬ IsTemp x

/-- Expressions avoid division and temporaries. -/
def FExpr.Supported : FExpr → Prop
  | .val v => v.Supported
  | .op v o e => v.Supported ∧ o ≠ .div ∧ e.Supported

/-- The variables a statement assigns. -/
def Stmt.Assigns : Stmt → String → Prop
  | .assign x _, y => x = y
  | .forLoop x _ _ b, y => x = y ∨ b.Assigns y
  | .ifThen _ b, y => b.Assigns y
  | .ifElse _ t e, y => t.Assigns y ∨ e.Assigns y
  | .seq s₁ s₂, y => s₁.Assigns y ∨ s₂.Assigns y

/-- Statements avoid division and temporaries, and loop bodies do not assign
their loop variable. -/
def Stmt.Supported : Stmt → Prop
  | .assign x e => ¬ IsTemp x ∧ e.Supported
  | .forLoop x _ _ b => ¬ IsTemp x ∧ ¬ b.Assigns x ∧ b.Supported
  | .ifThen c b => c.Supported ∧ b.Supported
  | .ifElse c t e => c.Supported ∧ t.Supported ∧ e.Supported
  | .seq s₁ s₂ => s₁.Supported ∧ s₂.Supported

/-- Canonical form: sequences nest to the right ("statement statements"). -/
def Stmt.Canonical : Stmt → Prop
  | .assign _ _ => True
  | .forLoop _ _ _ b => b.Canonical
  | .ifThen _ b => b.Canonical
  | .ifElse _ t e => t.Canonical ∧ e.Canonical
  | .seq (.seq _ _) _ => False
  | .seq s₁ s₂ => s₁.Canonical ∧ s₂.Canonical

/-- Compile a statement starting at label `pc`; control falls through to label
`pc + length`. -/
def Stmt.compile : Stmt → ℕ → List (Instr String ℕ)
  | .assign x e, pc =>
    let ce := e.compile 0 pc
    ce ++ [.alu2 .mov x (.var (temp 0)) (pc + ce.length + 1)]
  | .forLoop x lo hi body, pc =>
    -- pc: MOV x, #lo; pc+1: CMPGT x, #hi → end / pc+2; body; ADD x, x, #1; B pc+1
    let cb := body.compile (pc + 2)
    let stop := pc + 2 + cb.length + 2
    [.alu2 .mov x (.imm lo) (pc + 1), .cmp .gt (.var x) (.imm hi) stop (pc + 2)] ++ cb ++
      [.alu .add x (.var x) (.imm 1) (pc + 2 + cb.length + 1), .nop (pc + 1)]
  | .ifThen c body, pc =>
    let cc := c.compile 0 pc
    let cb := body.compile (pc + cc.length + 1)
    cc ++ [.cmp .eq (.var (temp 0)) (.imm 0) (pc + cc.length + 1 + cb.length)
      (pc + cc.length + 1)] ++ cb
  | .ifElse c t e, pc =>
    let cc := c.compile 0 pc
    let ct := t.compile (pc + cc.length + 1)
    let ce := e.compile (pc + cc.length + 1 + ct.length + 1)
    cc ++ [.cmp .eq (.var (temp 0)) (.imm 0) (pc + cc.length + 1 + ct.length + 1)
      (pc + cc.length + 1)] ++ ct ++
      [.nop (pc + cc.length + 1 + ct.length + 1 + ce.length)] ++ ce
  | .seq s₁ s₂, pc =>
    let c₁ := s₁.compile pc
    c₁ ++ s₂.compile (pc + c₁.length)

/-- The flowgraph of a code list: label `n` is the `n`th instruction, and labels
past the end exit. -/
def toFlowgraph (code : List (Instr String ℕ)) : Flowgraph String ℕ where
  entry := 0
  code n := code.getD n (.exit [])

/-! ## Temporaries -/

/-- Arithmetic on code labels and list lengths. -/
local macro "len_arith" : tactic =>
  `(tactic| first
    | omega
    | (simp only [List.length_append, List.length_cons, List.length_nil]; omega)
    | simp
    | (simp; omega))

/-! ## Execution helpers -/

section Exec

variable (G : Flowgraph String ℕ)

variable {G}

end Exec

/-- `l` is laid out in `G` from label `pc` on. -/
def CodeAt (G : Flowgraph String ℕ) (pc : ℕ) (l : List (Instr String ℕ)) : Prop :=
  ∀ i (hi : i < l.length), G.code (pc + i) = l[i]

section CodeAt

variable {G : Flowgraph String ℕ}

end CodeAt

/-! ## Evaluation lemmas -/

/-! ## Semantic lemmas -/

/-- The environment after `k` iterations of a `for` loop. -/
def loopEnv (x : String) (lo : ℤ) (b : Stmt) (σ : FEnv) (k : ℕ) : FEnv :=
  (List.range k).foldl (fun env j => b.run (Function.update env x (lo + j))) σ

/-! ## Correctness of compilation -/

/-- Compilation is correct: the compiled code halts, agreeing with the program
on every non-temporary variable. -/
theorem compile_correct (s : Stmt) (hs : s.Supported) (env : Env String) (mem : Mem) :
    ∃ t : Config String ℕ, (toFlowgraph (s.compile 0)).Steps ⟨0, env, mem⟩ [] t ∧
      (toFlowgraph (s.compile 0)).Halted t ∧ ∀ x, ¬ IsTemp x → t.env x = s.run env x := sorry

/-! ## Injectivity of compilation -/

/-- Sequences. -/
def Stmt.IsSeq : Stmt → Prop
  | .seq _ _ => True
  | _ => False

/-- Compilation is injective on supported canonical programs, so compiled code
has a unique decompilation. (Without canonical form it is not: `seq` is
compiled associatively.) -/
theorem compile_injective {s s' : Stmt} (hs : s.Supported) (hs' : s'.Supported)
    (hc : s.Canonical) (hc' : s'.Canonical) (h : s.compile 0 = s'.compile 0) : s = s' := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Decompilation
