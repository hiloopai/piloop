module

public import Mathlib.Data.Int.Bitwise
public import Mathlib.Data.Finset.Basic
public import Mathlib.Data.Finset.Union
public import Mathlib.Logic.Function.Basic

/-! # §1 3-address code and its semantics (notes pp. 4–6; slides pp. 42–46)

The intermediate code of Part A: 3-address instructions (notes p. 4, and the
course's `3addrcode.txt` summary) stored in a *flowgraph*, a graph whose nodes
are labelled with instructions (notes p. 4). Here a flowgraph over node labels
`ι` assigns an instruction to every node, and each instruction names its
successor nodes, so `ENTRY`, `ALU`, `MOV`, `LDI`, `STI`, `B` have one successor
and `CMP⟨cond⟩` has two (notes p. 4: "in a flowgraph they have two successor
edges").

The semantics is a small-step machine on configurations `(pc, env, mem)`: a
current node, an environment of integer variables (the virtual registers) and
a memory of integers addressed by integers (for `LDI`/`STI`). A step may emit an
output (`print`); the observable behaviour of a run is its output sequence and,
when it reaches `EXIT`, the values of the exit's result variables and the final
memory (notes p. 5: results are "stored in standard places, e.g. … res1").

## Deviations

* Values are mathematical integers rather than machine words.
* `CALL f` and `CALLI a` are not instructions of this IR; procedures and call
  graphs are modelled separately in `UnreachableCode` (§1.3).
* Indirect branches (`BI r`) are not modelled: successors are explicit.
* `print a` is an explicit output instruction, standing in for the notes'
  "I/O behaviour" (p. 8) and the slides' `print z` (p. 68).
* `EXIT` lists the variables that hold the procedure's results.
* `NOP` (the empty statement `l1: ;` of p. 8, and `B lab`) is an instruction.
  Dead-code elimination replaces a dead instruction by a `NOP`, keeping the
  graph's shape, rather than splicing it out (cf. CompCert's `Inop`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

/-- An operand: a variable (virtual register) or an immediate `#k`. -/
inductive Operand (V : Type*) where
  | var (x : V)
  | imm (k : ℤ)
  deriving DecidableEq

/-- Binary ALU operations (`ADD`, `SUB`, `MUL`, `AND`, `OR`, `XOR`). -/
inductive AluOp where
  | add | sub | mul | and | or | xor
  deriving DecidableEq

/-- Unary ALU operations (`MOV`, `NEG`, `NOT`): "ALU operations with one operand
ignored" (`3addrcode.txt`). -/
inductive UnOp where
  | mov | neg | not
  deriving DecidableEq

/-- Branch conditions of `CMP⟨cond⟩`. -/
inductive Cond where
  | eq | ne | lt | le | gt | ge
  deriving DecidableEq

/-- Integer meaning of a binary ALU operation. -/
def AluOp.eval : AluOp → ℤ → ℤ → ℤ
  | .add, a, b => a + b
  | .sub, a, b => a - b
  | .mul, a, b => a * b
  | .and, a, b => Int.land a b
  | .or, a, b => Int.lor a b
  | .xor, a, b => Int.lor (Int.land a (Int.lnot b)) (Int.land (Int.lnot a) b)

/-- Integer meaning of a unary ALU operation. -/
def UnOp.eval : UnOp → ℤ → ℤ
  | .mov, a => a
  | .neg, a => -a
  | .not, a => Int.lnot a

/-- Meaning of a branch condition. -/
def Cond.eval : Cond → ℤ → ℤ → Bool
  | .eq, a, b => decide (a = b)
  | .ne, a, b => decide (a ≠ b)
  | .lt, a, b => decide (a < b)
  | .le, a, b => decide (a ≤ b)
  | .gt, a, b => decide (a > b)
  | .ge, a, b => decide (a ≥ b)

/-- 3-address instructions over variables `V` with successor labels `ι`
(notes p. 4; `3addrcode.txt`). -/
inductive Instr (V : Type*) (ι : Type*) where
  /-- `ENTRY f`: the start of a procedure. -/
  | entry (next : ι)
  /-- The empty statement, or an unconditional branch `B lab`. -/
  | nop (next : ι)
  /-- `ALU dst, a, b`. -/
  | alu (op : AluOp) (dst : V) (a b : Operand V) (next : ι)
  /-- `ALU2 dst, a` (including `MOV dst, a`). -/
  | alu2 (op : UnOp) (dst : V) (a : Operand V) (next : ι)
  /-- `LDI dst, addr`: `dst = *addr`. -/
  | ldi (dst : V) (addr : Operand V) (next : ι)
  /-- `STI addr, val`: `*addr = val`. -/
  | sti (addr val : Operand V) (next : ι)
  /-- `CMP⟨cond⟩ a, b, lab`: two successors. -/
  | cmp (c : Cond) (a b : Operand V) (ifTrue ifFalse : ι)
  /-- `print a`: observable output. -/
  | print (a : Operand V) (next : ι)
  /-- `EXIT`, returning the values of `results` (the "standard places"). -/
  | exit (results : List V)
  deriving DecidableEq

/-- A flowgraph (notes p. 4): an entry node and an instruction at every node. -/
structure Flowgraph (V : Type*) (ι : Type*) where
  entry : ι
  code : ι → Instr V ι

/-- Environments: the values of the variables. -/
abbrev Env (V : Type*) := V → ℤ

/-- Memories, addressed by integers. -/
abbrev Mem := ℤ → ℤ

/-- Machine configurations. -/
structure Config (V : Type*) (ι : Type*) where
  pc : ι
  env : Env V
  mem : Mem

namespace Operand

variable {V : Type*}

/-- The value of an operand. -/
def eval (env : Env V) : Operand V → ℤ
  | .var x => env x
  | .imm k => k

/-- The variables an operand reads. -/
def vars : Operand V → Finset V
  | .var x => {x}
  | .imm _ => ∅

end Operand

namespace Instr

variable {V ι : Type*}

/-- Successor nodes, in order (`succ(n)` of notes p. 4). -/
def succs : Instr V ι → List ι
  | .entry n | .nop n | .alu _ _ _ _ n | .alu2 _ _ _ n | .ldi _ _ n | .sti _ _ n
    | .print _ n => [n]
  | .cmp _ _ _ t f => [t, f]
  | .exit _ => []

variable [DecidableEq V]

/-- `def(n)`: the variables an instruction defines (notes p. 8). -/
def defs : Instr V ι → Finset V
  | .alu _ d _ _ _ | .alu2 _ d _ _ | .ldi d _ _ => {d}
  | _ => ∅

/-- `ref(n)`: the variables an instruction references (notes p. 8). `STI` reads
both operands ("a STI instruction reads both its register operands",
`3addrcode.txt`); `EXIT` references its result variables. -/
def refs : Instr V ι → Finset V
  | .entry _ | .nop _ => ∅
  | .alu _ _ a b _ => a.vars ∪ b.vars
  | .alu2 _ _ a _ => a.vars
  | .ldi _ a _ => a.vars
  | .sti a b _ => a.vars ∪ b.vars
  | .cmp _ a b _ _ => a.vars ∪ b.vars
  | .print a _ => a.vars
  | .exit rs => rs.toFinset

/-- All variables an instruction mentions. -/
def vars (i : Instr V ι) : Finset V := i.defs ∪ i.refs

/-- One execution step of an instruction from `(env, mem)`: the next node, the
new environment and memory, and an optional output. `EXIT` does not step. -/
def exec (env : Env V) (mem : Mem) : Instr V ι → Option (ι × Env V × Mem × Option ℤ)
  | .entry n | .nop n => some (n, env, mem, none)
  | .alu op d a b n => some (n, Function.update env d (op.eval (a.eval env) (b.eval env)), mem, none)
  | .alu2 op d a n => some (n, Function.update env d (op.eval (a.eval env)), mem, none)
  | .ldi d a n => some (n, Function.update env d (mem (a.eval env)), mem, none)
  | .sti a b n => some (n, env, Function.update mem (a.eval env) (b.eval env), none)
  | .cmp c a b t f => some (if c.eval (a.eval env) (b.eval env) then t else f, env, mem, none)
  | .print a n => some (n, env, mem, some (a.eval env))
  | .exit _ => none

end Instr

namespace Flowgraph

variable {V ι : Type*} (G : Flowgraph V ι)

/-- `succ(n) = {n' | (n, n') ∈ edges(G)}` (notes p. 4). -/
def succ (n : ι) : Set ι := {m | m ∈ (G.code n).succs}

/-- `pred(n) = {n' | (n', n) ∈ edges(G)}` (notes p. 4). -/
def pred (n : ι) : Set ι := {m | n ∈ (G.code m).succs}

theorem mem_pred_iff_mem_succ (m n : ι) : m ∈ G.pred n ↔ n ∈ G.succ m := sorry

variable [DecidableEq V]

/-- One machine step: `none` at `EXIT`, otherwise the output and the next
configuration. -/
def step (s : Config V ι) : Option (Option ℤ × Config V ι) :=
  ((G.code s.pc).exec s.env s.mem).map fun r => (r.2.2.2, ⟨r.1, r.2.1, r.2.2.1⟩)

/-- Multi-step execution, accumulating the outputs. -/
inductive Steps : Config V ι → List ℤ → Config V ι → Prop
  | refl (s : Config V ι) : Steps s [] s
  | head {s s' s'' : Config V ι} {o : Option ℤ} {w : List ℤ} :
      G.step s = some (o, s') → Steps s' w s'' → Steps s (o.toList ++ w) s''

/-- A configuration at an `EXIT`. -/
def Halted (s : Config V ι) : Prop := ∃ rs, G.code s.pc = .exit rs

/-- The result of a halted run: its outputs, the values of the exit's result
variables, and the final memory. -/
structure Outcome where
  output : List ℤ
  results : List ℤ
  mem : Mem

/-- The result values read by the instruction at `s.pc` if it is an `EXIT`. -/
def resultValues (s : Config V ι) : List ℤ :=
  match G.code s.pc with
  | .exit rs => rs.map s.env
  | _ => []

/-- Finite observations of a run from `s`: every output prefix it can produce.
These capture the I/O behaviour of terminating and non-terminating runs. -/
def traces (s : Config V ι) : Set (List ℤ) := {w | ∃ s', G.Steps s w s'}

/-- Outcomes of the halting runs from `s`. -/
def outcomes (s : Config V ι) : Set Outcome :=
  {o | ∃ s', G.Steps s o.output s' ∧ G.Halted s' ∧ o.results = G.resultValues s' ∧
    o.mem = s'.mem}

/-- Two configurations are observationally equivalent: same traces and outcomes. -/
def ObsEquiv (G' : Flowgraph V ι) (s t : Config V ι) : Prop :=
  G.traces s = G'.traces t ∧ G.outcomes s = G'.outcomes t

/-- A halted configuration does not step, and a non-halted one does. -/
theorem step_eq_none_iff (s : Config V ι) : G.step s = none ↔ G.Halted s := sorry

/-- Executions are deterministic up to prefix: two runs from the same
configuration produce comparable outputs. -/
theorem steps_output_prefix {s t₁ t₂ : Config V ι} {w₁ w₂ : List ℤ}
    (h₁ : G.Steps s w₁ t₁) (h₂ : G.Steps s w₂ t₂) : w₁ <+: w₂ ∨ w₂ <+: w₁ := sorry

section Simulation

variable (G' : Flowgraph V ι) (R : Config V ι → Config V ι → Prop)

/-- A forward simulation `R` between `G` and `G'` transports runs of `G`. -/
theorem steps_of_sim
    (hfwd : ∀ {s t s' : Config V ι} {o : Option ℤ}, R s t → G.step s = some (o, s') →
      ∃ t', G'.step t = some (o, t') ∧ R s' t')
    {s s' t : Config V ι} {w : List ℤ} (h : G.Steps s w s') (hst : R s t) :
    ∃ t', G'.Steps t w t' ∧ R s' t' := sorry

/-- A forward simulation that also preserves halting transports runs of `G'`
back to `G` (both machines are deterministic). -/
theorem steps_of_sim_rev
    (hfwd : ∀ {s t s' : Config V ι} {o : Option ℤ}, R s t → G.step s = some (o, s') →
      ∃ t', G'.step t = some (o, t') ∧ R s' t')
    (hnone : ∀ {s t : Config V ι}, R s t → G.step s = none → G'.step t = none)
    {t t' s : Config V ι} {w : List ℤ} (h : G'.Steps t w t') (hst : R s t) :
    ∃ s', G.Steps s w s' ∧ R s' t' := sorry

/-- A forward simulation that preserves halting and the halting observations
(result values and memory) yields observational equivalence. -/
theorem obsEquiv_of_sim
    (hfwd : ∀ {s t s' : Config V ι} {o : Option ℤ}, R s t → G.step s = some (o, s') →
      ∃ t', G'.step t = some (o, t') ∧ R s' t')
    (hnone : ∀ {s t : Config V ι}, R s t → G.step s = none → G'.step t = none)
    (hres : ∀ {s t : Config V ι}, R s t → G.Halted s →
      G.resultValues s = G'.resultValues t ∧ s.mem = t.mem)
    {s t : Config V ι} (hst : R s t) : G.ObsEquiv G' s t := sorry

end Simulation

/-- All variables mentioned anywhere in a finite flowgraph. -/
def vars [Fintype ι] : Finset V := Finset.univ.biUnion fun n => (G.code n).vars

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
