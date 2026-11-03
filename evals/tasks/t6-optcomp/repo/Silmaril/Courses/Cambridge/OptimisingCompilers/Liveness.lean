module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
public import Silmaril.Semantics.Dataflow.Iteration
public import Mathlib.Data.Fintype.Pi
public import Mathlib.Algebra.Order.BigOperators.Group.Finset

/-! # §2 Live variable analysis (notes pp. 8–9; slides pp. 54–59)

* `SynLive` is *syntactic liveness* (notes p. 8): `x` is syntactically live at
  `n` if there is a path from `n` to a node referencing `x` along which `x` is
  not defined.
* `SemLive` is *semantic liveness* (notes p. 8): changing the value of `x` at
  `n` can change the I/O behaviour of some execution from `n`.
* `liveEquations` are the dataflow equations
  `live(n) = (⋃ s ∈ succ(n), live(s)) \ def(n) ∪ ref(n)` (notes p. 8), an
  instance of the generic framework `Silmaril.Semantics.Dataflow.Equations`, and `live`
  is their least solution.

Main results:

* `mem_live_iff_synLive`: the least solution is exactly syntactic liveness;
* `semLive_subset_synLive`: `sem-live(n) ⊆ syn-live(n)` (notes p. 8), and more
  generally `semLive_subset_of_isSafe`: any safe assignment over-approximates
  semantic liveness ("any solution of these dataflow equations is safe", p. 8);
* `agree_step`: the simulation lemma behind both, also used by dead-code
  elimination: configurations agreeing on the live variables step together;
* `lva`: the executable algorithm (p. 9) on finite flowgraphs, with
  `lva_isSafe` and `lva_eq_live` (it computes the least solution).

Ambiguous references through pointers (p. 9) do not arise: memory is separate
from the variables, and `LDI`/`STI` only reference their operand variables.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

open Silmaril.Semantics.Dataflow

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- The liveness transfer function `S ↦ S \ def(n) ∪ ref(n)` (notes p. 8). -/
def liveTransfer (n : ι) : Set V →o Set V where
  toFun S := S \ ↑(G.code n).defs ∪ ↑(G.code n).refs
  monotone' _ _ h := Set.union_subset_union_left _ (Set.sdiff_subset_sdiff_left h)

/-- The LVA dataflow equations: a backward framework over `succ` (notes p. 8). -/
def liveEquations : Equations ι (Set V) where
  nbrs := G.succ
  transfer := G.liveTransfer

/-- `live(n)`: the least solution of the LVA equations (notes p. 9). -/
noncomputable def live : ι → Set V := G.liveEquations.least

/-- `out-live(n) = ⋃ s ∈ succ(n), live(s)` (notes p. 8). -/
def outLive (live : ι → Set V) (n : ι) : Set V := ⋃ s ∈ G.succ n, live s

/-- The two-stage form of the equations (notes p. 8):
`in-live(n) = out-live(n) \ def(n) ∪ ref(n)`. -/
theorem liveEquations_step_apply (x : ι → Set V) (n : ι) :
    G.liveEquations.step x n = G.outLive x n \ ↑(G.code n).defs ∪ ↑(G.code n).refs := rfl

/-- A safe assignment contains the references of each node. -/
theorem refs_subset_of_isSafe {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    (n : ι) : (↑(G.code n).refs : Set V) ⊆ live n :=
  fun _ hx => hlive n (Set.mem_union_right _ hx)

/-- A safe assignment propagates liveness backwards across non-defining nodes. -/
theorem mem_of_isSafe_succ {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {n m : ι} (hm : m ∈ G.succ n) {x : V} (hx : x ∈ live m) (hd : x ∉ (G.code n).defs) :
    x ∈ live n :=
  hlive n (Set.mem_union_left _ ⟨Set.mem_iUnion₂.2 ⟨m, hm, hx⟩, hd⟩)

/-- Syntactic liveness (notes p. 8): a path from `n` to a node referencing `x`
that contains no definition of `x` (before the reference). -/
inductive SynLive : ι → V → Prop
  | ref {n : ι} {x : V} : x ∈ (G.code n).refs → SynLive n x
  | succ {n m : ι} {x : V} : m ∈ G.succ n → x ∉ (G.code n).defs → SynLive m x → SynLive n x

/-- Configurations that agree on the variables of `S`. -/
def AgreeOn (S : Set V) (s t : Config V ι) : Prop :=
  s.pc = t.pc ∧ s.mem = t.mem ∧ ∀ x ∈ S, s.env x = t.env x

/-- The simulation lemma: if `live` is safe and two configurations agree on
`live(pc)`, then they step to configurations agreeing on `live(pc')`, with
the same output. -/
theorem agree_step {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {s t s' : Config V ι} {o : Option ℤ}
    (hst : AgreeOn (live s.pc) s t) (hs : G.step s = some (o, s')) :
    ∃ t', G.step t = some (o, t') ∧ AgreeOn (live s'.pc) s' t' := by
  obtain ⟨pc, env, mem⟩ := s
  obtain ⟨pc', env', mem'⟩ := t
  obtain ⟨hpc, hmem, hag⟩ := hst
  dsimp only at hpc hmem hag
  subst pc' mem'
  have hrefs : ∀ x ∈ (G.code pc).refs, env x = env' x := fun x hx =>
    hag x (G.refs_subset_of_isSafe hlive pc (Finset.mem_coe.2 hx))
  have hsucc : ∀ m ∈ (G.code pc).succs, ∀ x ∈ live m, x ∉ (G.code pc).defs →
      env x = env' x :=
    fun m hm x hx hd => hag x (G.mem_of_isSafe_succ hlive hm hx hd)
  have hop : ∀ a : Operand V, a.vars ⊆ (G.code pc).refs → a.eval env = a.eval env' := by
    intro a ha
    cases a with
    | var x => exact hrefs x (ha (by simp [Operand.vars]))
    | imm k => rfl
  cases hc : G.code pc with
  | entry m | nop m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    refine ⟨⟨m, env', mem⟩, by simp [step, hc, Instr.exec], rfl, rfl, fun x hx => ?_⟩
    exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs])
  | alu op d a b m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    have hb := hop b (by simp [hc, Instr.refs])
    refine ⟨⟨m, Function.update env' d (op.eval (a.eval env') (b.eval env')), mem⟩,
      by simp [step, hc, Instr.exec], rfl, rfl, fun x hx => ?_⟩
    by_cases hxd : x = d
    · subst hxd
      simp [ha, hb]
    · simp only [Function.update_of_ne hxd]
      exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs, hxd])
  | alu2 op d a m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    refine ⟨⟨m, Function.update env' d (op.eval (a.eval env')), mem⟩,
      by simp [step, hc, Instr.exec], rfl, rfl, fun x hx => ?_⟩
    by_cases hxd : x = d
    · subst hxd
      simp [ha]
    · simp only [Function.update_of_ne hxd]
      exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs, hxd])
  | ldi d a m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    refine ⟨⟨m, Function.update env' d (mem (a.eval env')), mem⟩,
      by simp [step, hc, Instr.exec], rfl, rfl, fun x hx => ?_⟩
    by_cases hxd : x = d
    · subst hxd
      simp [ha]
    · simp only [Function.update_of_ne hxd]
      exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs, hxd])
  | sti a b m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    have hb := hop b (by simp [hc, Instr.refs])
    refine ⟨⟨m, env', Function.update mem (a.eval env') (b.eval env')⟩,
      by simp [step, hc, Instr.exec], rfl, by simp [ha, hb], fun x hx => ?_⟩
    exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs])
  | cmp c a b tt ff =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    have hb := hop b (by simp [hc, Instr.refs])
    refine ⟨⟨if c.eval (a.eval env') (b.eval env') then tt else ff, env', mem⟩,
      by simp [step, hc, Instr.exec], by simp [ha, hb], rfl, fun x hx => ?_⟩
    exact hsucc _ (by split_ifs <;> simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs])
  | print a m =>
    simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
    obtain ⟨rfl, rfl⟩ := hs
    have ha := hop a (by simp [hc, Instr.refs])
    refine ⟨⟨m, env', mem⟩, by simp [step, hc, Instr.exec, ha], rfl, rfl, fun x hx => ?_⟩
    exact hsucc m (by simp [hc, Instr.succs]) x hx (by simp [hc, Instr.defs])
  | exit rs => simp [step, hc, Instr.exec] at hs

/-- At an `EXIT` reached in agreement, two flowgraphs with the same exit return
the same result values. -/
theorem resultValues_eq_of_agree {live : ι → Set V} (hlive : G.liveEquations.IsSafe live)
    {G' : Flowgraph V ι} {s t : Config V ι} (hst : AgreeOn (live s.pc) s t) {rs : List V}
    (hrs : G.code s.pc = .exit rs) (hrs' : G'.code t.pc = .exit rs) :
    G.resultValues s = G'.resultValues t := by
  simp only [resultValues, hrs, hrs']
  apply List.map_congr_left
  intro x hx
  exact hst.2.2 x (G.refs_subset_of_isSafe hlive s.pc (by simp [hrs, Instr.refs, hx]))

/-- Semantic liveness (notes p. 8): there is an execution starting at `n` whose
I/O behaviour is changed by changing the value of `x`. -/
def SemLive (n : ι) (x : V) : Prop :=
  ∃ (env : Env V) (mem : Mem) (v : ℤ),
    ¬ G.ObsEquiv G ⟨n, env, mem⟩ ⟨n, Function.update env x v, mem⟩

/-- `sem-live(n) ⊆ syn-live(n)` (notes p. 8). -/
theorem semLive_subset_synLive {n : ι} {x : V} (h : G.SemLive n x) : G.SynLive n x := sorry

/-! ## The algorithm (notes p. 9) -/

section Algorithm

variable [Fintype ι] [DecidableEq ι]

/-- One round of the LVA equations on finite sets. -/
def liveStep (x : ι → Finset V) (n : ι) : Finset V :=
  ((G.code n).succs.toFinset.biUnion x \ (G.code n).defs) ∪ (G.code n).refs

/-- The fuel bound: the lattice of assignments below `G.vars` has height
`card ι * card G.vars`. -/
def lvaFuel : ℕ := Fintype.card ι * G.vars.card + 1

/-- The LVA algorithm (notes p. 9): start from `{}` everywhere and re-apply the
equations until nothing changes. If the fuel runs out (it never does,
`lva_converges`), fall back on the safe assignment `G.vars` everywhere. -/
def lva : ι → Finset V :=
  (iterateUntilStable G.liveStep G.lvaFuel ⊥).getD fun _ => G.vars

set_option linter.unusedSectionVars false in
/-- `liveStep` is the set form of the equations. -/
theorem coe_liveStep (x : ι → Finset V) :
    (fun n => (G.liveStep x n : Set V)) = G.liveEquations.step fun n => (x n : Set V) := by
  funext n
  ext y
  simp [liveStep, liveEquations_step_apply, outLive, succ]

omit [Fintype ι] in
theorem liveStep_mono : Monotone G.liveStep := fun x y h n => by
  intro z hz
  simp only [liveStep, Finset.mem_union, Finset.mem_sdiff, Finset.mem_biUnion] at hz ⊢
  rcases hz with ⟨⟨m, hm, hz⟩, hd⟩ | hz
  · exact Or.inl ⟨⟨m, hm, h m hz⟩, hd⟩
  · exact Or.inr hz

theorem liveStep_subset_vars {x : ι → Finset V} (hx : ∀ n, x n ⊆ G.vars) (n : ι) :
    G.liveStep x n ⊆ G.vars := by
  have hv : (G.code n).vars ⊆ G.vars :=
    Finset.subset_biUnion_of_mem (fun n => (G.code n).vars) (Finset.mem_univ n)
  intro y hy
  simp only [liveStep, Finset.mem_union, Finset.mem_sdiff, Finset.mem_biUnion] at hy
  rcases hy with ⟨⟨m, -, hm⟩, -⟩ | hy
  · exact hx m hm
  · exact hv (Finset.mem_union_right _ hy)

theorem iterate_liveStep_subset_vars (k : ℕ) (n : ι) : (G.liveStep^[k] ⊥) n ⊆ G.vars := by
  induction k generalizing n with
  | zero => simp
  | succ k ih =>
    rw [Function.iterate_succ_apply']
    exact G.liveStep_subset_vars ih n

omit [DecidableEq V] [DecidableEq ι] in
theorem sum_card_lt_of_lt {x y : ι → Finset V} (h : x ≤ y) (hne : x ≠ y) :
    ∑ n, (x n).card < ∑ n, (y n).card := by
  obtain ⟨n, hn⟩ := Function.ne_iff.1 hne
  exact Finset.sum_lt_sum (fun i _ => Finset.card_le_card (h i))
    ⟨n, Finset.mem_univ _, Finset.card_lt_card (lt_of_le_of_ne (h n) hn)⟩

/-- The loop terminates within `lvaFuel` rounds. -/
theorem lva_converges : (iterateUntilStable G.liveStep G.lvaFuel ⊥).isSome := by
  have hchain : ∀ k, G.liveStep^[k] ⊥ ≤ G.liveStep^[k + 1] ⊥ := fun k =>
    Monotone.monotone_iterate_of_le_map G.liveStep_mono bot_le (Nat.le_succ k)
  have hbound : ∀ k, ∑ n, ((G.liveStep^[k] ⊥) n).card ≤ Fintype.card ι * G.vars.card := by
    intro k
    calc ∑ n, ((G.liveStep^[k] ⊥) n).card ≤ ∑ _n : ι, G.vars.card :=
          Finset.sum_le_sum fun n _ => Finset.card_le_card (G.iterate_liveStep_subset_vars k n)
      _ = Fintype.card ι * G.vars.card := by simp
  have hfix : ∀ k j, k ≤ j → G.liveStep (G.liveStep^[k] ⊥) = G.liveStep^[k] ⊥ →
      G.liveStep (G.liveStep^[j] ⊥) = G.liveStep^[j] ⊥ := by
    intro k j hkj h
    obtain ⟨d, rfl⟩ := Nat.exists_eq_add_of_le hkj
    rw [add_comm, Function.iterate_add_apply, Function.iterate_fixed h]
    exact h
  have hgrow : ∀ k, (∀ j < k, G.liveStep (G.liveStep^[j] ⊥) ≠ G.liveStep^[j] ⊥) →
      k ≤ ∑ n, ((G.liveStep^[k] ⊥) n).card := by
    intro k
    induction k with
    | zero => intro _; exact Nat.zero_le _
    | succ k ih =>
      intro h
      have h1 := ih fun j hj => h j (Nat.lt_succ_of_lt hj)
      have h2 := sum_card_lt_of_lt (hchain k) (by
        rw [Function.iterate_succ_apply']
        exact (h k (Nat.lt_succ_self k)).symm)
      exact Nat.succ_le_of_lt (lt_of_le_of_lt h1 h2)
  have hB : G.liveStep (G.liveStep^[Fintype.card ι * G.vars.card] ⊥) =
      G.liveStep^[Fintype.card ι * G.vars.card] ⊥ := by
    by_contra hne
    have h1 := hgrow (Fintype.card ι * G.vars.card + 1) fun j hj hj' =>
      hne (hfix j _ (Nat.lt_succ_iff.1 hj) hj')
    have h2 := hbound (Fintype.card ι * G.vars.card + 1)
    omega
  exact iterateUntilStable_isSome_of_isFixedPt G.liveStep hB

/-- The Finset iterates are the set iterates of the LVA equations. -/
theorem coe_iterate_liveStep (k : ℕ) :
    (fun n => ((G.liveStep^[k] ⊥) n : Set V)) = G.liveEquations.step^[k] ⊥ := by
  induction k with
  | zero => funext n; simp
  | succ k ih =>
    rw [Function.iterate_succ_apply', Function.iterate_succ_apply', ← ih, coe_liveStep]

theorem coe_lva_eq_live : (fun n => (G.lva n : Set V)) = G.live := by
  obtain ⟨y, hy⟩ := Option.isSome_iff_exists.1 G.lva_converges
  have hlva : G.lva = y := by simp [lva, hy]
  have hfx := iterateUntilStable_isFixedPt _ hy
  obtain ⟨k, -, rfl⟩ := iterateUntilStable_eq_iterate _ hy
  rw [hlva, G.coe_iterate_liveStep k]
  show _ = G.liveEquations.step.lfp
  apply iterate_eq_lfp_of_isFixedPt
  rw [← G.coe_iterate_liveStep k, ← G.coe_liveStep, hfx]

/-- The algorithm's result is safe. -/
theorem lva_isSafe : G.liveEquations.IsSafe fun n => (G.lva n : Set V) := by
  rw [G.coe_lva_eq_live]
  exact G.liveEquations.least_isSafe

/-- The algorithm computes the least solution (notes p. 9: "it always
terminates with the least solution"). -/
theorem lva_eq_live : (fun n => (G.lva n : Set V)) = G.live := G.coe_lva_eq_live

end Algorithm

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
