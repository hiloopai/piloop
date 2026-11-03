module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.AvailableExpressions
public import Mathlib.Data.Fin.VecNotation
public import Mathlib.Tactic.FinCases
public import Mathlib.Data.Fintype.Basic

/-! # §7 Static single assignment form (notes pp. 15–16; slides pp. 84–88)

"SSA form is a compilation technique to enable repeated assignments to the same
variable (in flowgraph-style code) to be replaced by code in which each
variable occurs (statically) as a destination exactly once" (notes p. 16).

* `Flowgraph.IsSSA`: every variable is defined at most once;
* straight-line code (`Assign`): `toSSA` numbers instances `vᵢ` of each `v`,
  incrementing on every update (notes p. 16: `v = 3; v = v+1; v = v+w; w = v*2`
  becomes `v₇ = 3; v₈ = v₇+1; v₉ = v₈+w₃; w₄ = v₉*2`), with
  `toSSA_isSSA` and `toSSA_correct`;
* φ-functions at path merges: `PhiGraph` has, at each node, φ-assignments
  `x := φ(xₚ for each predecessor p)` evaluated on arrival from `p`, with a
  semantics that remembers the previous node.

This module uses the course's SSA presentation, separately from other IRs.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- SSA: each variable is the destination of at most one node. -/
def IsSSA : Prop := ∀ x n m, x ∈ (G.code n).defs → x ∈ (G.code m).defs → n = m

end Flowgraph

/-! ## Straight-line code -/

/-- A straight-line assignment `dst := rhs`. -/
structure Assign (V : Type*) where
  dst : V
  rhs : Rhs V

namespace Assign

variable {V : Type*} [DecidableEq V]

/-- Run straight-line code. -/
def run : List (Assign V) → Env V → Env V
  | [], env => env
  | a :: as, env => run as (Function.update env a.dst (a.rhs.eval env))

/-- Rename an operand to the current instance of its variable. -/
def renameOperand (idx : V → ℕ) : Operand V → Operand (V × ℕ)
  | .var x => .var (x, idx x)
  | .imm k => .imm k

/-- Rename an expression to current instances. -/
def renameRhs (idx : V → ℕ) : Rhs V → Rhs (V × ℕ)
  | .bin op a b => .bin op (renameOperand idx a) (renameOperand idx b)
  | .un op a => .un op (renameOperand idx a)

/-- Translation to SSA (notes p. 16): `idx v` is the current instance of `v`;
each update of `v` uses instance `idx v + 1`. Returns the code and the final
instance numbering. -/
def toSSA : (V → ℕ) → List (Assign V) → List (Assign (V × ℕ)) × (V → ℕ)
  | idx, [] => ([], idx)
  | idx, a :: as =>
    let idx' := Function.update idx a.dst (idx a.dst + 1)
    let r := toSSA idx' as
    (⟨(a.dst, idx' a.dst), renameRhs idx a.rhs⟩ :: r.1, r.2)

/-- Static single assignment: no destination is repeated. -/
def IsSSA (as : List (Assign V)) : Prop := (as.map Assign.dst).Nodup

theorem toSSA_nil (idx : V → ℕ) : toSSA idx [] = ([], idx) := sorry

/-- The translation is in SSA form. -/
theorem toSSA_isSSA (idx : V → ℕ) (as : List (Assign V)) : IsSSA (toSSA idx as).1 := sorry

/-- The translation is correct: if instance `(v, idx v)` initially holds the value
of `v`, then after running, instance `(v, idx' v)` holds the final value of `v`. -/
theorem toSSA_correct (idx : V → ℕ) (as : List (Assign V)) (env : Env V) (senv : Env (V × ℕ))
    (h : ∀ v, senv (v, idx v) = env v) (v : V) :
    run (toSSA idx as).1 senv (v, (toSSA idx as).2 v) = run as env v := sorry

/-- The notes' example: `v = 3; v = v+1; v = v+w; w = v*2`. -/
def notesExample : List (Assign String) :=
  [⟨"v", .un .mov (.imm 3)⟩, ⟨"v", .bin .add (.var "v") (.imm 1)⟩,
    ⟨"v", .bin .add (.var "v") (.var "w")⟩, ⟨"w", .bin .mul (.var "v") (.imm 2)⟩]

/-- With next indices 7 for `v` and 4 for `w` (current 6 and 3), the translation
is `v₇ = 3; v₈ = v₇+1; v₉ = v₈+w₃; w₄ = v₉*2` (notes p. 16). -/
theorem notesExample_toSSA :
    (toSSA (fun x => if x = "v" then 6 else if x = "w" then 3 else 0) notesExample).1 =
      [⟨("v", 7), .un .mov (.imm 3)⟩,
        ⟨("v", 8), .bin .add (.var ("v", 7)) (.imm 1)⟩,
        ⟨("v", 9), .bin .add (.var ("v", 8)) (.var ("w", 3))⟩,
        ⟨("w", 4), .bin .mul (.var ("v", 9)) (.imm 2)⟩] := sorry

end Assign

/-! ## φ-functions -/

/-- A flowgraph with φ-assignments at node entry (notes p. 16): on arrival at
`n` from `p`, each `(x, args)` in `phis n` sets `x` to the value of `args p`,
all simultaneously. -/
structure PhiGraph (V : Type*) (ι : Type*) extends Flowgraph V ι where
  phis : ι → List (V × (ι → V))

namespace PhiGraph

variable {V ι : Type*} [DecidableEq V] (P : PhiGraph V ι)

/-- Simultaneous φ-assignment on arrival at `n` from `p`. -/
def enter (n p : ι) (env : Env V) : Env V := fun x =>
  match (P.phis n).find? (fun b => decide (b.1 = x)) with
  | some (_, args) => env (args p)
  | none => env x

/-- One step: run the instruction at `pc`, then the φ-assignments of the next
node for the edge taken. -/
def step (s : Config V ι) : Option (Option ℤ × Config V ι) :=
  (P.toFlowgraph.step s).map fun r => (r.1, { r.2 with env := P.enter r.2.pc s.pc r.2.env })

/-- SSA for φ-graphs: each variable is defined by at most one instruction or
φ-assignment. -/
def IsSSA : Prop :=
  P.toFlowgraph.IsSSA ∧ (∀ n x, x ∈ (P.phis n).map Prod.fst → ∀ m, x ∉ (P.code m).defs) ∧
    ∀ n m x, x ∈ (P.phis n).map Prod.fst → x ∈ (P.phis m).map Prod.fst → n = m

end PhiGraph

/-- The notes' φ example (p. 16):
`{ if (p) { v₄ = v₃+1; v₅ = v₄+w; } else v₆ = v₃−1; } v₇ = φ(v₅, v₆); w = v₇*2`. -/
def phiExample : PhiGraph String (Fin 6) where
  entry := 0
  code := ![.cmp .ne (.var "p") (.imm 0) 1 3,
    .alu .add "v4" (.var "v3") (.imm 1) 2,
    .alu .add "v5" (.var "v4") (.var "w") 4,
    .alu .sub "v6" (.var "v3") (.imm 1) 4,
    .alu .mul "w2" (.var "v7") (.imm 2) 5,
    .exit ["w2"]]
  phis := ![[], [], [], [], [("v7", fun p => if p = 2 then "v5" else "v6")], []]

/-- The φ example is in SSA form. -/
theorem phiExample_isSSA : phiExample.IsSSA := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers
