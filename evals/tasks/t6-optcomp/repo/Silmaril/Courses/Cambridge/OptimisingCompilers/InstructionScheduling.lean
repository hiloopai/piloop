module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
public import Silmaril.Courses.Cambridge.OptimisingCompilers.AvailableExpressions
public import Mathlib.Data.List.Perm.Basic
public import Mathlib.Data.List.FinRange
public import Mathlib.Data.List.OfFn

/-! # Part C: Instruction scheduling (notes pp. 36–38; slides pp. 117–126)

"Observe that two instructions may be permuted if neither writes to a register
read or written by the other. We define a graph (actually a DAG), whose nodes
are instructions within a basic block. Place an edge from instruction `a` to
instruction `b` if `a` occurs before `b` in the original instruction sequence
and if `a` and `b` cannot be permuted. […] In general any topological sort of
this DAG gives a valid scheduling sequence" (notes p. 36).

A basic block is a list of straight-line instructions; `Conflicts` is the
"cannot be permuted" relation, extended to memory (a store conflicts with any
load or store) and output (prints stay in order). A schedule is a permutation
of the block's positions that keeps every conflicting pair in order
(`IsValidSchedule`), and `schedule_correct` says it computes the same
environment, memory and outputs.

`listSchedule` is the greedy algorithm of p. 36–37: repeatedly emit a minimal
element of the remaining DAG, preferring by a heuristic; it always produces a
valid schedule (`listSchedule_valid`). `stalls` counts the load-use
interlocks of a single-issue pipeline with a one-cycle load delay (p. 36),
which the heuristics try to minimise.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.InstructionScheduling

/-- Straight-line instructions: those of the IR with a single successor that is
ignored (`ι := Unit`). -/
abbrev BInstr (V : Type*) := Instr V Unit

variable {V : Type*} [DecidableEq V]

/-- Memory and output effects of an instruction. -/
def isStore : BInstr V → Bool
  | .sti .. => true
  | _ => false

def isLoad : BInstr V → Bool
  | .ldi .. => true
  | _ => false

def isPrint : BInstr V → Bool
  | .print .. => true
  | _ => false

/-- Two instructions cannot be permuted (notes p. 36): one writes a register the
other reads or writes, or both touch memory and one stores, or both print. -/
def conflicts (a b : BInstr V) : Bool :=
  decide (a.defs ∩ (b.refs ∪ b.defs)).Nonempty || decide (b.defs ∩ a.refs).Nonempty ||
    ((isStore a || isStore b) && (isStore a || isLoad a) && (isStore b || isLoad b)) ||
    (isPrint a && isPrint b)

/-- `conflicts` as a proposition. -/
def Conflicts (a b : BInstr V) : Prop := conflicts a b = true

/-- Run a block: environment, memory, and outputs. Non-straight-line
instructions are skipped. -/
def runBlock : List (BInstr V) → Env V → Mem → Env V × Mem × List ℤ
  | [], env, mem => (env, mem, [])
  | i :: is, env, mem =>
    match i.exec env mem with
    | some (_, env', mem', o) =>
      let r := runBlock is env' mem'
      (r.1, r.2.1, o.toList ++ r.2.2)
    | none => runBlock is env mem

/-- A schedule of block `b`: a permutation `σ` of its positions (`σ k` is the
original position of the `k`th scheduled instruction) that keeps conflicting
pairs in their original order. -/
def IsValidSchedule (b : List (BInstr V)) (σ : Equiv.Perm (Fin b.length)) : Prop :=
  ∀ k k' : Fin b.length, k < k' → Conflicts (b.get (σ k')) (b.get (σ k)) → σ k' < σ k → False

/-- The scheduled block. -/
def applySchedule (b : List (BInstr V)) (σ : Equiv.Perm (Fin b.length)) : List (BInstr V) :=
  List.ofFn fun k => b.get (σ k)

/-- The effect of a single instruction: new environment, memory and outputs. -/
def stepI (i : BInstr V) (env : Env V) (mem : Mem) : Env V × Mem × List ℤ :=
  match i.exec env mem with
  | some (_, env', mem', o) => (env', mem', o.toList)
  | none => (env, mem, [])

theorem runBlock_cons (i : BInstr V) (is : List (BInstr V)) (env : Env V) (mem : Mem) :
    runBlock (i :: is) env mem =
      ((runBlock is (stepI i env mem).1 (stepI i env mem).2.1).1,
        (runBlock is (stepI i env mem).1 (stepI i env mem).2.1).2.1,
        (stepI i env mem).2.2 ++ (runBlock is (stepI i env mem).1 (stepI i env mem).2.1).2.2) := by
  simp only [runBlock, stepI]
  split <;> simp_all

/-- Running a block only depends on the semantics of its tail. -/
theorem runBlock_cons_congr (i : BInstr V) {l l' : List (BInstr V)}
    (h : runBlock l = runBlock l') : runBlock (i :: l) = runBlock (i :: l') := by
  funext env mem
  simp only [runBlock_cons, h]

omit [DecidableEq V] in
private theorem singleton_inter_nonempty' [DecidableEq V] {a : V} {s : Finset V} :
    ({a} ∩ s).Nonempty ↔ a ∈ s := by
  rw [Finset.singleton_inter]; split_ifs with h <;> simp [h]

/-- Two non-conflicting instructions commute (notes p. 36). -/
theorem stepI_comm (x y : BInstr V) (h : ¬ Conflicts x y) (env : Env V) (mem : Mem) :
    (stepI y (stepI x env mem).1 (stepI x env mem).2.1).1 =
        (stepI x (stepI y env mem).1 (stepI y env mem).2.1).1 ∧
      (stepI y (stepI x env mem).1 (stepI x env mem).2.1).2.1 =
        (stepI x (stepI y env mem).1 (stepI y env mem).2.1).2.1 ∧
      (stepI x env mem).2.2 ++ (stepI y (stepI x env mem).1 (stepI x env mem).2.1).2.2 =
        (stepI y env mem).2.2 ++ (stepI x (stepI y env mem).1 (stepI y env mem).2.1).2.2 := by
  simp only [Conflicts, Bool.not_eq_true] at h
  cases x <;> cases y <;>
    simp_all [conflicts, Instr.defs, Instr.refs, isStore, isLoad, isPrint, stepI, Instr.exec,
      singleton_inter_nonempty',
      Operand.eval_update_of_not_mem]
  all_goals exact Function.update_comm (by rintro rfl; simp_all) _ _ _

theorem runBlock_swap (x y : BInstr V) (h : ¬ Conflicts x y) (rest : List (BInstr V)) :
    runBlock (x :: y :: rest) = runBlock (y :: x :: rest) := by
  funext env mem
  obtain ⟨h₁, h₂, h₃⟩ := stepI_comm x y h env mem
  simp only [runBlock_cons, h₁, h₂, ← List.append_assoc, h₃]

/-- Move an instruction to the front past instructions it does not conflict with. -/
theorem runBlock_move_front (x : BInstr V) (post : List (BInstr V)) :
    ∀ pre : List (BInstr V), (∀ i ∈ pre, ¬ Conflicts i x) →
      runBlock (pre ++ x :: post) = runBlock (x :: (pre ++ post))
  | [], _ => rfl
  | i :: pre, h => by
    rw [List.cons_append, List.cons_append,
      runBlock_cons_congr i (runBlock_move_front x post pre fun j hj => h j (by simp [hj])),
      runBlock_swap i x (h i (by simp))]

/-- Any valid schedule (topological sort of the DAG) has the same semantics. -/
theorem schedule_correct (b : List (BInstr V)) (σ : Equiv.Perm (Fin b.length))
    (hσ : IsValidSchedule b σ) (env : Env V) (mem : Mem) :
    runBlock (applySchedule b σ) env mem = runBlock b env mem := sorry

/-- Load-use stalls of a single-issue pipeline with a one-cycle load delay: a
load immediately followed by an instruction reading its destination. -/
def stalls : List (BInstr V) → ℕ
  | a :: b :: rest =>
    (if isLoad a && decide (a.defs ∩ b.refs).Nonempty then 1 else 0) + stalls (b :: rest)
  | _ => 0

/-- The list-scheduling algorithm (notes pp. 36–37): from the remaining positions,
emit the first candidate (no earlier remaining instruction conflicts with it)
that minimises `prio`. -/
def listSchedule (b : List (BInstr V)) (prio : List (BInstr V) → BInstr V → ℕ) :
    List (Fin b.length) :=
  go b.length (List.finRange b.length) []
where
  /-- Positions of `rest` that may be emitted next. -/
  candidates (rest : List (Fin b.length)) : List (Fin b.length) :=
    rest.filter fun k => rest.all fun k' =>
      !(decide (k' < k) && conflicts (b.get k') (b.get k))
  /-- The first element minimising `f`. -/
  pickMin (f : Fin b.length → ℕ) : List (Fin b.length) → Option (Fin b.length)
    | [] => none
    | a :: as =>
      match pickMin f as with
      | none => some a
      | some c => if f c < f a then some c else some a
  /-- Emit candidates until none remain. -/
  go : ℕ → List (Fin b.length) → List (Fin b.length) → List (Fin b.length)
    | 0, _, acc => acc.reverse
    | fuel + 1, rest, acc =>
      match pickMin (fun k => prio (acc.reverse.map b.get) (b.get k)) (candidates rest) with
      | some k => go fuel (rest.erase k) (k :: acc)
      | none => acc.reverse

omit [DecidableEq V] in
theorem listSchedule.pickMin_mem (b : List (BInstr V)) (f : Fin b.length → ℕ) :
    ∀ {l : List (Fin b.length)} {c : Fin b.length}, listSchedule.pickMin b f l = some c → c ∈ l := by
  intro l
  induction l with
  | nil => intro c h; simp [listSchedule.pickMin] at h
  | cons a as ih =>
    intro c h
    cases hp : listSchedule.pickMin b f as with
    | none =>
      simp only [listSchedule.pickMin, hp, Option.some.injEq] at h
      simp [h]
    | some c' =>
      simp only [listSchedule.pickMin, hp] at h
      split_ifs at h
      · simp only [Option.some.injEq] at h
        subst h
        simp [ih hp]
      · simp only [Option.some.injEq] at h
        simp [h]

omit [DecidableEq V] in
theorem listSchedule.pickMin_eq_none (b : List (BInstr V)) (f : Fin b.length → ℕ)
    {l : List (Fin b.length)} (h : listSchedule.pickMin b f l = none) : l = [] := by
  cases l with
  | nil => rfl
  | cons a as =>
    exfalso
    cases hp : listSchedule.pickMin b f as with
    | none => simp [listSchedule.pickMin, hp] at h
    | some c =>
      simp only [listSchedule.pickMin, hp] at h
      split_ifs at h

theorem listSchedule.mem_candidates (b : List (BInstr V)) {rest : List (Fin b.length)}
    {k : Fin b.length} (hk : k ∈ listSchedule.candidates b rest) :
    k ∈ rest ∧ ∀ k' ∈ rest, k' < k → conflicts (b.get k') (b.get k) = false := by
  obtain ⟨h₁, h₂⟩ := List.mem_filter.1 hk
  refine ⟨h₁, fun k' hk' hlt => ?_⟩
  have := List.all_eq_true.1 h₂ k' hk'
  simpa [hlt] using this

omit [DecidableEq V] in
/-- A nonempty list of positions has a least element. -/
theorem listSchedule.exists_min_mem (b : List (BInstr V)) :
    ∀ {l : List (Fin b.length)}, l ≠ [] → ∃ k ∈ l, ∀ k' ∈ l, ¬ k' < k
  | [], h => absurd rfl h
  | a :: as, _ => by
    by_cases has : as = []
    · subst has
      refine ⟨a, by simp, fun k' hk' hlt => ?_⟩
      rw [List.mem_singleton] at hk'
      subst hk'
      exact Nat.lt_irrefl _ hlt
    · obtain ⟨m, hm, hmin⟩ := listSchedule.exists_min_mem b has
      by_cases ham : a < m
      · refine ⟨a, by simp, fun k' hk' hlt => ?_⟩
        rcases List.mem_cons.1 hk' with rfl | hk'
        · exact Nat.lt_irrefl _ hlt
        · exact hmin k' hk' (Nat.lt_trans hlt ham)
      · refine ⟨m, List.mem_cons_of_mem _ hm, fun k' hk' hlt => ?_⟩
        rcases List.mem_cons.1 hk' with rfl | hk'
        · exact ham hlt
        · exact hmin k' hk' hlt

theorem listSchedule.exists_mem_candidates (b : List (BInstr V)) {rest : List (Fin b.length)}
    (hne : rest ≠ []) : ∃ k, k ∈ listSchedule.candidates b rest := by
  obtain ⟨k, hk, hmin⟩ := listSchedule.exists_min_mem b hne
  refine ⟨k, List.mem_filter.2 ⟨hk, List.all_eq_true.2 fun k' hk' => ?_⟩⟩
  simp [hmin k' hk']

/-- A list keeps conflicting pairs in their original order. -/
def SchedOrd (b : List (BInstr V)) (L : List (Fin b.length)) : Prop :=
  ∀ i j, i ∈ L → j ∈ L → i < j → Conflicts (b.get i) (b.get j) → L.idxOf i < L.idxOf j

/-- List scheduling emits every position once, keeping conflicting pairs in
order: it is a topological sort of the DAG. -/
theorem listSchedule_valid (b : List (BInstr V)) (prio : List (BInstr V) → BInstr V → ℕ) :
    (listSchedule b prio).Perm (List.finRange b.length) ∧
      ∀ i j, i < j → Conflicts (b.get i) (b.get j) →
        (listSchedule b prio).idxOf i < (listSchedule b prio).idxOf j := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.InstructionScheduling
