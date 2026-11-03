module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.AvailableExpressions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Execution
public import Silmaril.Courses.Cambridge.OptimisingCompilers.PlusSim

/-! # §5 Uses of AVAIL: common-subexpression elimination and copy propagation
(notes pp. 13–14; slides pp. 79–83)

CSE (notes p. 13), for an expression `e = a ⊕ b` and a new temporary `t`:

* "for each node `n` containing `x := a ⊕ b` with `a ⊕ b` available at `n`":
  replace `n : x := a ⊕ b` with `n : x := t`;
* "on each path scanning backwards from `n`, for the first occurrence of `a ⊕ b`
  (say `n′ : y := a ⊕ b`) … replace `n′` with two instructions
  `n′ : t := a ⊕ b; n″ : y := t`".

Here every occurrence `y := e` is split (a superset of the first occurrences on
each path, which is simpler and equally correct). The new nodes `n″` are
`Sum.inr n′`; original nodes are `Sum.inl n`.

*Copy propagation* (notes p. 14) replaces a use of `x` by `y` where the copy
`x := y` is known to hold. Which copies hold is the must gen/kill problem
`copyProblem`: `MOV x, y` generates the fact `(x, y)`, and any definition of
`x` or `y` kills it.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

/-- Replace `x` by `y` in an operand. -/
def Operand.subst {V : Type*} [DecidableEq V] (x y : V) : Operand V → Operand V
  | .var z => if z = x then .var y else .var z
  | .imm k => .imm k

namespace Instr

variable {V ι κ : Type*}

/-- Relabel the successors of an instruction. -/
def mapLabels (f : ι → κ) : Instr V ι → Instr V κ
  | .entry n => .entry (f n)
  | .nop n => .nop (f n)
  | .alu op d a b n => .alu op d a b (f n)
  | .alu2 op d a n => .alu2 op d a (f n)
  | .ldi d a n => .ldi d a (f n)
  | .sti a b n => .sti a b (f n)
  | .cmp c a b t e => .cmp c a b (f t) (f e)
  | .print a n => .print a (f n)
  | .exit rs => .exit rs

/-- The instruction `dst := rhs` with successor `n`. -/
def ofAssignment (dst : V) (n : ι) : Rhs V → Instr V ι
  | .bin op a b => .alu op dst a b n
  | .un op a => .alu2 op dst a n

/-- The successor of an ALU instruction. -/
def aluNext? : Instr V ι → Option ι
  | .alu _ _ _ _ n | .alu2 _ _ _ n => some n
  | _ => none

/-- Replace the uses of `x` by `y` in an instruction (definitions unchanged). -/
def substUses [DecidableEq V] (x y : V) : Instr V ι → Instr V ι
  | .alu op d a b n => .alu op d (a.subst x y) (b.subst x y) n
  | .alu2 op d a n => .alu2 op d (a.subst x y) n
  | .ldi d a n => .ldi d (a.subst x y) n
  | .sti a b n => .sti (a.subst x y) (b.subst x y) n
  | .cmp c a b t f => .cmp c (a.subst x y) (b.subst x y) t f
  | .print a n => .print (a.subst x y) n
  | .exit rs => .exit (rs.map fun z => if z = x then y else z)
  | i => i

end Instr

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- CSE of `e` into the temporary `t` at the nodes `targets` (notes p. 13). -/
def cse (e : Rhs V) (t : V) (targets : ι → Bool) : Flowgraph V (ι ⊕ ι) where
  entry := .inl G.entry
  code
    | .inl n =>
      match (G.code n).assignment?, (G.code n).aluNext? with
      | some (x, e'), some m =>
        if e' = e then
          if targets n then .alu2 .mov x (.var t) (.inl m)
          else Instr.ofAssignment t (.inr n) e
        else (G.code n).mapLabels .inl
      | _, _ => (G.code n).mapLabels .inl
    | .inr n =>
      match (G.code n).assignment?, (G.code n).aluNext? with
      | some (y, _), some m => .alu2 .mov y (.var t) (.inl m)
      | _, _ => .exit []

/-- `t` is fresh for `G`: no instruction mentions it. -/
def Fresh (t : V) : Prop := ∀ n, t ∉ (G.code n).vars

/-! ## Correctness of CSE

The proof is a one-or-two-step simulation (`PlusSim`). Original node `n` is
related to `Sum.inl n` with the same memory, the same values on every variable
other than `t`, and `t` holding the value of `e` whenever `e` is available at
`n`. A split node `n′ : y := e` is matched by the two steps `t := e; y := t`. -/

section CSE

variable {e : Rhs V} {t : V} {targets : ι → Bool}

variable (e t) in
/-- The CSE simulation relation. -/
def cseRel (s : Config V ι) (c : Config V (ι ⊕ ι)) : Prop :=
  c.pc = .inl s.pc ∧ c.mem = s.mem ∧ (∀ z ≠ t, c.env z = s.env z) ∧
    (e ∈ G.avail s.pc → c.env t = e.eval s.env)

end CSE

/-! ## Copy propagation (notes p. 14) -/

/-- The copy `x := y` at an instruction. -/
def copyGen (n : ι) : Set (V × V) :=
  {c | ∃ m, G.code n = .alu2 .mov c.1 (.var c.2) m ∧ c.1 ≠ c.2}

/-- Definitions of `x` or `y` kill the copy `x := y`. -/
def copyKill (n : ι) : Set (V × V) := {c | c.1 ∈ (G.code n).defs ∨ c.2 ∈ (G.code n).defs}

/-- Available copies: a forward must problem. -/
def copyProblem : GenKill ι (V × V) where
  nbrs := G.pred
  gen := G.copyGen
  kill := G.copyKill

/-- Copies known to hold on entry to `n`. -/
noncomputable def copies (n : ι) : Set (V × V) := G.copyProblem.mustIn n

/-- Copy propagation of `x := y` at the nodes `sites`. -/
def copyProp (x y : V) (sites : ι → Bool) : Flowgraph V ι where
  entry := G.entry
  code n := if sites n then (G.code n).substUses x y else G.code n

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
