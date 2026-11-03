module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Liveness
public import Mathlib.Data.Fin.VecNotation

/-! # §4 Dead-code elimination by LVA (notes p. 10; slides pp. 68–69)

"Recall that code is dead when its result goes unused; if the variable `x` is
not live on exit from an instruction which assigns some value to `x`, then the
whole instruction is dead. […] dead code with no live side effects is useless
and may be removed. […] The program resulting from this transformation will
remain correct" (slides, Lecture 5, p. 68).

An instruction is *removable* under a liveness assignment `live` when it is a
side-effect-free definition (`ALU`, `ALU2`/`MOV`, `LDI`) of a variable that is
not live at its unique successor. `dceWith live` replaces every removable
instruction by `NOP`; `dce` uses the computed LVA solution `lva`.

Main results:

* `dceWith_obsEquiv`: for any safe `live`, `dceWith live` is observationally
  equivalent to the original from every configuration (same output traces,
  same halting outcomes);
* `dce_obsEquiv`: hence for `dce`, "the program resulting from this
  transformation will remain correct";
* `dce_code`: every instruction is kept or becomes a `NOP` ("smaller", p. 68);
* `exists_dce_dce_ne`: "successive iterations may yield further improvements"
  (p. 68): one pass of `dce` need not be idempotent.

The proof is the lock-step simulation `AgreeOn (live pc)` of the liveness
module: a removable instruction only changes a variable that is dead at the
next node.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers

namespace Instr

variable {V ι : Type*}

/-- The instruction is a side-effect-free definition whose result is not live at
its successor (slides p. 68). -/
def Removable (live : ι → Set V) : Instr V ι → Prop
  | .alu _ d _ _ n | .alu2 _ d _ n | .ldi d _ n => d ∉ live n
  | _ => False

/-- Executable test for `Removable` against finite liveness sets. -/
def isRemovable [DecidableEq V] (live : ι → Finset V) : Instr V ι → Bool
  | .alu _ d _ _ n | .alu2 _ d _ n | .ldi d _ n => decide (d ∉ live n)
  | _ => false

theorem isRemovable_iff [DecidableEq V] (live : ι → Finset V) (i : Instr V ι) :
    i.isRemovable live = true ↔ i.Removable fun n => (live n : Set V) := sorry

/-- The successor of a single-successor instruction (only used on removable
instructions). -/
def next? : Instr V ι → Option ι
  | .entry n | .nop n | .alu _ _ _ _ n | .alu2 _ _ _ n | .ldi _ _ n | .sti _ _ n
    | .print _ n => some n
  | .cmp _ _ _ _ _ | .exit _ => none

/-- Replace a removable instruction by `NOP`. -/
def eraseIfRemovable [DecidableEq V] (live : ι → Finset V) (i : Instr V ι) : Instr V ι :=
  match i.isRemovable live, i.next? with
  | true, some n => .nop n
  | _, _ => i

end Instr

namespace Flowgraph

variable {V ι : Type*} [DecidableEq V] (G : Flowgraph V ι)

/-- Dead-code elimination against a given liveness assignment. -/
def dceWith (live : ι → Finset V) : Flowgraph V ι where
  entry := G.entry
  code n := (G.code n).eraseIfRemovable live

/-- `dceWith` keeps each instruction or replaces a removable one by `NOP`. -/
theorem dceWith_code (live : ι → Finset V) (n : ι) :
    (G.dceWith live).code n = G.code n ∨
      ((G.code n).Removable (fun m => (live m : Set V)) ∧
        ∃ m, (G.code n).next? = some m ∧ (G.dceWith live).code n = .nop m) := by
  show (G.code n).eraseIfRemovable live = G.code n ∨
    ((G.code n).Removable (fun m => (live m : Set V)) ∧
      ∃ m, (G.code n).next? = some m ∧ (G.code n).eraseIfRemovable live = .nop m)
  generalize G.code n = i
  cases i with
  | alu op d a b m =>
    by_cases hd : d ∈ live m <;>
      simp [Instr.eraseIfRemovable, Instr.isRemovable, Instr.next?, Instr.Removable, hd]
  | alu2 op d a m =>
    by_cases hd : d ∈ live m <;>
      simp [Instr.eraseIfRemovable, Instr.isRemovable, Instr.next?, Instr.Removable, hd]
  | ldi d a m =>
    by_cases hd : d ∈ live m <;>
      simp [Instr.eraseIfRemovable, Instr.isRemovable, Instr.next?, Instr.Removable, hd]
  | _ => left; rfl

/-- `dceWith` keeps every `EXIT`. -/
theorem dceWith_code_exit (live : ι → Finset V) {n : ι} {rs : List V}
    (h : G.code n = .exit rs) : (G.dceWith live).code n = .exit rs := by
  show (G.code n).eraseIfRemovable live = _
  rw [h]
  rfl

/-- The lock-step simulation behind DCE: agreement on the live variables is
preserved by a step of the original and the corresponding step of the
transformed flowgraph. -/
theorem dceWith_agree_step (live : ι → Finset V)
    (hlive : G.liveEquations.IsSafe fun n => (live n : Set V))
    {s t s' : Config V ι} {o : Option ℤ}
    (hst : AgreeOn (live s.pc : Set V) s t) (hs : G.step s = some (o, s')) :
    ∃ t', (G.dceWith live).step t = some (o, t') ∧ AgreeOn (live s'.pc : Set V) s' t' := by
  rcases G.dceWith_code live t.pc with hc' | ⟨hrem, m', hnext, hc'⟩
  · obtain ⟨t', ht, hR⟩ := G.agree_step hlive hst hs
    refine ⟨t', ?_, hR⟩
    rw [← ht]
    simp only [step, hc']
  · obtain ⟨pc, env, mem⟩ := s
    obtain ⟨pc', env', mem'⟩ := t
    obtain ⟨hpc, hmem, hag⟩ := hst
    dsimp only at hpc hmem hag hrem hnext hc' ⊢
    subst pc' mem'
    cases hc : G.code pc with
    | alu op d a b m =>
      rw [hc] at hrem hnext
      have hm : m' = m := by simpa [Instr.next?] using hnext.symm
      rw [hm] at hc'
      simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
      obtain ⟨rfl, rfl⟩ := hs
      refine ⟨⟨m, env', mem⟩, by simp [step, hc', Instr.exec], rfl, rfl, fun x hx => ?_⟩
      have hxd : x ≠ d := by
        rintro rfl
        simp only [Instr.Removable, Finset.mem_coe] at hrem
        exact hrem hx
      simp only [Function.update_of_ne hxd]
      exact hag x (G.mem_of_isSafe_succ hlive (by simp [succ, hc, Instr.succs]) hx
        (by simp [hc, Instr.defs, hxd]))
    | alu2 op d a m =>
      rw [hc] at hrem hnext
      have hm : m' = m := by simpa [Instr.next?] using hnext.symm
      rw [hm] at hc'
      simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
      obtain ⟨rfl, rfl⟩ := hs
      refine ⟨⟨m, env', mem⟩, by simp [step, hc', Instr.exec], rfl, rfl, fun x hx => ?_⟩
      have hxd : x ≠ d := by
        rintro rfl
        simp only [Instr.Removable, Finset.mem_coe] at hrem
        exact hrem hx
      simp only [Function.update_of_ne hxd]
      exact hag x (G.mem_of_isSafe_succ hlive (by simp [succ, hc, Instr.succs]) hx
        (by simp [hc, Instr.defs, hxd]))
    | ldi d a m =>
      rw [hc] at hrem hnext
      have hm : m' = m := by simpa [Instr.next?] using hnext.symm
      rw [hm] at hc'
      simp only [step, hc, Instr.exec, Option.map_some, Option.some.injEq, Prod.mk.injEq] at hs
      obtain ⟨rfl, rfl⟩ := hs
      refine ⟨⟨m, env', mem⟩, by simp [step, hc', Instr.exec], rfl, rfl, fun x hx => ?_⟩
      have hxd : x ≠ d := by
        rintro rfl
        simp only [Instr.Removable, Finset.mem_coe] at hrem
        exact hrem hx
      simp only [Function.update_of_ne hxd]
      exact hag x (G.mem_of_isSafe_succ hlive (by simp [succ, hc, Instr.succs]) hx
        (by simp [hc, Instr.defs, hxd]))
    | _ =>
      rw [hc] at hrem
      simp [Instr.Removable] at hrem

/-- DCE is correct for every safe liveness assignment: the transformed
flowgraph is observationally equivalent from every configuration. -/
theorem dceWith_obsEquiv (live : ι → Finset V)
    (hlive : G.liveEquations.IsSafe fun n => (live n : Set V)) (s : Config V ι) :
    G.ObsEquiv (G.dceWith live) s s := by
  refine G.obsEquiv_of_sim (G.dceWith live) (fun s t => AgreeOn (live s.pc : Set V) s t)
    (fun hR hs => G.dceWith_agree_step live hlive hR hs) ?_ ?_ ⟨rfl, rfl, fun _ _ => rfl⟩
  · intro s t hR hs
    rw [G.step_eq_none_iff] at hs
    obtain ⟨rs, hrs⟩ := hs
    rw [(G.dceWith live).step_eq_none_iff]
    exact ⟨rs, by rw [← hR.1]; exact G.dceWith_code_exit live hrs⟩
  · intro s t hR hh
    obtain ⟨rs, hrs⟩ := hh
    exact ⟨G.resultValues_eq_of_agree hlive hR hrs
      (by rw [← hR.1]; exact G.dceWith_code_exit live hrs), hR.2.1⟩

section Algorithm

variable [Fintype ι] [DecidableEq ι]

/-- Dead-code elimination driven by the LVA algorithm (slides p. 68). -/
def dce : Flowgraph V ι := G.dceWith G.lva

/-- "The program resulting from this transformation will remain correct"
(slides p. 68). -/
theorem dce_obsEquiv (s : Config V ι) : G.ObsEquiv G.dce s s :=
  G.dceWith_obsEquiv G.lva G.lva_isSafe s

/-- DCE removes exactly the instructions that are removable under the least
liveness solution. -/
theorem dce_code (n : ι) :
    G.dce.code n = G.code n ∨
      ((G.code n).Removable G.live ∧ ∃ m, (G.code n).next? = some m ∧ G.dce.code n = .nop m) := sorry

/-- Iterated DCE is correct. -/
theorem dce_iterate_obsEquiv (k : ℕ) (s : Config V ι) : G.ObsEquiv ((fun H : Flowgraph V ι => H.dce)^[k] G) s s := sorry

end Algorithm

/-- "Successive iterations may yield further improvements" (slides p. 68): the
slide's example `a = x+11; b = y+13; c = a*b; print z`, where one pass removes
`c` and the next removes `a` and `b`. -/
def slideExample : Flowgraph String (Fin 6) where
  entry := 0
  code := ![.alu .add "a" (.var "x") (.imm 11) 1,
    .alu .add "b" (.var "y") (.imm 13) 2,
    .alu .mul "c" (.var "a") (.var "b") 3,
    .print (.var "z") 4,
    .exit [],
    .exit []]

end Flowgraph

end Silmaril.Courses.Cambridge.OptimisingCompilers
