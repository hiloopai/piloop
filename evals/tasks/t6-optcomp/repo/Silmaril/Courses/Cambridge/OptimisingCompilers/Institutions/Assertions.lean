module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Signature
public import Silmaril.Courses.Cambridge.OptimisingCompilers.RegisterAllocation

/-! # The institution of state assertions

The *dataflow facts* of the course, read as sentences about a single machine
state `(env, mem)`:

* a constant fact `x = k` is `cmp .eq (var x) (imm k)`;
* an available-expressions fact "`x` holds `a op b`" (notes §3, pp. 9–10) is
  `cmp .eq (var x) (alu op a b)`;
* a copy fact "`x = y`" of copy propagation (notes §5, pp. 13–14) is
  `cmp .eq (var x) (var y)`;
* an interval fact `lo ≤ x ≤ hi` (abstract interpretation, notes §11, p. 20)
  is a conjunction of two comparisons.

`Assn V` is the closure of comparisons between expressions `Term V` under
`true`, negation and conjunction. The institution `assnLogic` has signatures
`RegSig` (register files with injective renamings), sentences `Assn V`, models
the states `Env V × Mem` (a discrete category), and satisfaction `Assn.Holds`.
The satisfaction condition is `Assn.holds_rename`: an assertion renamed along
`σ` holds in `(env, mem)` iff the original holds in the reduct `(env ∘ σ, mem)`.

Analyses whose facts are assertions at each node are theories of the
transition institution (`Institutions.Transitions`), via the comorphism
`assnToTrans` (an assertion about the source state of a transition).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

namespace Operand

variable {V R : Type*}

/-- Evaluation commutes with renaming: the renamed operand reads the reduct. -/
@[simp] theorem eval_rename (σ : V → R) (env : Env R) (a : Operand V) :
    (a.rename σ).eval env = a.eval (env ∘ σ) := by
  cases a <;> rfl

@[simp] theorem rename_id (a : Operand V) : a.rename id = a := by
  cases a <;> rfl

theorem rename_comp {W : Type*} (σ : V → R) (τ : R → W) (a : Operand V) :
    a.rename (τ ∘ σ) = (a.rename σ).rename τ := by
  cases a <;> rfl

end Operand

namespace Institutions

/-- Expressions over registers and memory: the right-hand sides of 3-address
instructions, nested. -/
inductive Term (V : Type) : Type where
  /-- A variable or an immediate. -/
  | opd (a : Operand V)
  /-- A binary ALU operation. -/
  | alu (op : AluOp) (t u : Term V)
  /-- A unary ALU operation. -/
  | un (op : UnOp) (t : Term V)
  /-- A memory read `*t`. -/
  | load (t : Term V)

/-- Assertions: comparisons of expressions closed under `true`, `¬` and `∧`. -/
inductive Assn (V : Type) : Type where
  | tt
  | cmp (c : Cond) (t u : Term V)
  | not (φ : Assn V)
  | and (φ ψ : Assn V)

namespace Term

variable {V R W : Type}

/-- Rename the registers of an expression. -/
def rename (σ : V → R) : Term V → Term R
  | .opd a => .opd (a.rename σ)
  | .alu op t u => .alu op (t.rename σ) (u.rename σ)
  | .un op t => .un op (t.rename σ)
  | .load t => .load (t.rename σ)

/-- The value of an expression in a state. -/
def eval (env : Env V) (mem : Mem) : Term V → ℤ
  | .opd a => a.eval env
  | .alu op t u => op.eval (t.eval env mem) (u.eval env mem)
  | .un op t => op.eval (t.eval env mem)
  | .load t => mem (t.eval env mem)

theorem rename_id (t : Term V) : t.rename id = t := by
  induction t with
  | opd a => exact congrArg Term.opd (Operand.rename_id a)
  | alu op t u iht ihu => exact congrArg₂ (Term.alu op) iht ihu
  | un op t ih => exact congrArg (Term.un op) ih
  | load t ih => exact congrArg Term.load ih

theorem rename_comp (σ : V → R) (τ : R → W) (t : Term V) :
    t.rename (τ ∘ σ) = (t.rename σ).rename τ := by
  induction t with
  | opd a => exact congrArg Term.opd (Operand.rename_comp σ τ a)
  | alu op t u iht ihu => exact congrArg₂ (Term.alu op) iht ihu
  | un op t ih => exact congrArg (Term.un op) ih
  | load t ih => exact congrArg Term.load ih

/-- Evaluation commutes with renaming. -/
theorem eval_rename (σ : V → R) (env : Env R) (mem : Mem) (t : Term V) :
    (t.rename σ).eval env mem = t.eval (env ∘ σ) mem := by
  induction t with
  | opd a => exact Operand.eval_rename σ env a
  | alu op t u iht ihu => exact congrArg₂ op.eval iht ihu
  | un op t ih => exact congrArg op.eval ih
  | load t ih => exact congrArg mem ih

end Term

namespace Assn

variable {V R W : Type}

/-- Rename the registers of an assertion. -/
def rename (σ : V → R) : Assn V → Assn R
  | .tt => .tt
  | .cmp c t u => .cmp c (t.rename σ) (u.rename σ)
  | .not φ => .not (φ.rename σ)
  | .and φ ψ => .and (φ.rename σ) (ψ.rename σ)

/-- Satisfaction of an assertion by a state. -/
def Holds (env : Env V) (mem : Mem) : Assn V → Prop
  | .tt => True
  | .cmp c t u => c.eval (t.eval env mem) (u.eval env mem) = true
  | .not φ => ¬ φ.Holds env mem
  | .and φ ψ => φ.Holds env mem ∧ ψ.Holds env mem

theorem rename_id (φ : Assn V) : φ.rename id = φ := by
  induction φ with
  | tt => rfl
  | cmp c t u => exact congrArg₂ (Assn.cmp c) (Term.rename_id t) (Term.rename_id u)
  | not φ ih => exact congrArg Assn.not ih
  | and φ ψ ih₁ ih₂ => exact congrArg₂ Assn.and ih₁ ih₂

theorem rename_comp (σ : V → R) (τ : R → W) (φ : Assn V) :
    φ.rename (τ ∘ σ) = (φ.rename σ).rename τ := by
  induction φ with
  | tt => rfl
  | cmp c t u => exact congrArg₂ (Assn.cmp c) (Term.rename_comp σ τ t) (Term.rename_comp σ τ u)
  | not φ ih => exact congrArg Assn.not ih
  | and φ ψ ih₁ ih₂ => exact congrArg₂ Assn.and ih₁ ih₂

/-- The satisfaction condition of `assnLogic`. -/
theorem holds_rename (σ : V → R) (env : Env R) (mem : Mem) (φ : Assn V) :
    (φ.rename σ).Holds env mem ↔ φ.Holds (env ∘ σ) mem := by
  induction φ with
  | tt => exact Iff.rfl
  | cmp c t u =>
    show c.eval _ _ = true ↔ c.eval _ _ = true
    rw [Term.eval_rename, Term.eval_rename]
  | not φ ih => exact not_congr ih
  | and φ ψ ih₁ ih₂ => exact and_congr ih₁ ih₂

end Assn

/-- Machine states without a program counter: the models of `assnLogic`. -/
abbrev State (V : Type) : Type := Env V × Mem

/-- The reduct of a state along a renaming. -/
def State.reduct {V R : Type} (σ : V → R) (s : State R) : State V := (s.1 ∘ σ, s.2)

/-- The institution of state assertions over register files. -/
def assnLogic : StateLogic.{0} where
  Sen A := Assn A.V
  map σ := Assn.rename (RegSig.ren σ)
  map_id _ := Assn.rename_id
  map_comp σ τ := Assn.rename_comp (RegSig.ren σ) (RegSig.ren τ)
  St A := State A.V
  reduct σ := State.reduct (RegSig.ren σ)
  reduct_id _ _ := rfl
  reduct_comp _ _ _ := rfl
  Holds s φ := φ.Holds s.1 s.2
  holds_map σ s φ := Assn.holds_rename (RegSig.ren σ) s.1 s.2 φ

end Institutions

end Silmaril.Courses.Cambridge.OptimisingCompilers
