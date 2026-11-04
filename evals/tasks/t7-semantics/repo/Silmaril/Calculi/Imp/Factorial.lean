module

public import Silmaril.Calculi.Imp.BigStep
public import Silmaril.Semantics.Domain.ScottInduction
import Mathlib.Tactic.Ring

/-! # The factorial loop

The running example of the notes (§1.2, Example 13 and Example 16): with two
locations `X` and `Y`, the program

  `while X > 0 do (Y := X * Y; X := X - 1)`     (3)

denotes the least fixed point of

  `F(w)[X ↦ x, Y ↦ y] = [X ↦ x, Y ↦ y]` if `x ≤ 0`,
  `F(w)[X ↦ x, Y ↦ y] = w[X ↦ x - 1, Y ↦ x ⋅ y]` if `x > 0`.

We compute its Kleene approximations `wₙ = Fⁿ(⊥)` and their union `w∞` as
tabulated on p. 10, and prove the partial correctness assertion of Example 16
by Scott induction.

Two small corrections to the notes:

* The table for `wₙ` on p. 10 holds for `n ≥ 1` only: `w₀ = ⊥` is undefined
  everywhere, while the table would make it defined when `x < 0`.
* In the stability step of Example 16 the notes write
  `F(w)[X ↦ x, Y ↦ y](Y) = x ⋅ y` when `x > 0`. The value is really
  `w[X ↦ x - 1, Y ↦ x ⋅ y](Y)`, which is non-negative because `w ∈ S` and
  `x ⋅ y ≥ 0`.
-/

@[expose] public section

namespace Silmaril.Calculi.Imp.Factorial


open OmegaCompletePartialOrder Silmaril.Semantics.Domain

/-- §1.2: the locations `X` and `Y` of the running example. -/
inductive Loc where
  | X
  | Y
  deriving DecidableEq

open Loc

/-- The state `[X ↦ x, Y ↦ y]`. -/
def st (x y : ℤ) : State Loc
  | X => x
  | Y => y

@[simp] theorem st_X (x y : ℤ) : st x y X = x := rfl

@[simp] theorem st_Y (x y : ℤ) : st x y Y = y := rfl

/-- Every state is of the form `[X ↦ x, Y ↦ y]`. -/
theorem eq_st (s : State Loc) : s = st (s X) (s Y) := by
  funext l
  cases l <;> rfl

@[simp] theorem update_st_X (x y v : ℤ) : Function.update (st x y) X v = st v y := by
  funext l
  cases l <;> rfl

@[simp] theorem update_st_Y (x y v : ℤ) : Function.update (st x y) Y v = st x v := by
  funext l
  cases l <;> rfl

/-- The loop guard `X > 0`. -/
def guard : Bexp Loc := .lt (.num 0) (.loc X)

/-- The loop body `Y := X * Y; X := X - 1`. -/
def body : Prog Loc :=
  .seq (.assign Y (.mul (.loc X) (.loc Y))) (.assign X (.sub (.loc X) (.num 1)))

/-- §1.2, program (3): `while X > 0 do (Y := X * Y; X := X - 1)`. -/
def factorialProg : Prog Loc := .while guard body

/-- §1.2: the functional `F = F_{⟦X > 0⟧, ⟦Y := X * Y; X := X - 1⟧}`. -/
noncomputable def factF :
    (State Loc → Part (State Loc)) →𝒄 (State Loc → Part (State Loc)) :=
  F B⟦guard⟧ C⟦body⟧

/-- §1.2: the explicit description of `F` on `[X ↦ x, Y ↦ y]`. -/
theorem factF_st (w : State Loc → Part (State Loc)) (x y : ℤ) :
    factF w (st x y) = if x ≤ 0 then Part.some (st x y) else w (st (x - 1) (x * y)) := by
  rw [factF, F_apply]
  by_cases hx : x ≤ 0
  · have hx' : ¬ 0 < x := not_lt.2 hx
    simp [guard, Bexp.denote, Aexp.denote, hx, hx']
  · simp [guard, body, Bexp.denote, Aexp.denote, hx, lt_of_not_ge hx]

/-- Example 13: the program denotes `fix F`. -/
theorem denote_factorialProg : C⟦factorialProg⟧ = fix factF := rfl

/-- §1.2: the Kleene approximations `wₙ = Fⁿ(⊥)`. -/
noncomputable def approx (n : ℕ) : State Loc → Part (State Loc) := factF^[n] ⊥

theorem approx_zero : approx 0 = ⊥ := rfl

theorem approx_succ (n : ℕ) : approx (n + 1) = factF (approx n) :=
  Function.iterate_succ_apply' _ _ _

/-- The table for `wₙ` (p. 10) in the form `w_{m+1}`. -/
theorem approx_succ_st (m : ℕ) (x y : ℤ) :
    approx (m + 1) (st x y) =
      if x < 0 then Part.some (st x y)
      else if x < m + 1 then Part.some (st 0 (x.toNat.factorial * y))
      else Part.none := by
  induction m generalizing x y with
  | zero =>
      rw [approx_succ, factF_st]
      rcases lt_trichotomy x 0 with hx | rfl | hx
      · simp [hx, hx.le]
      · simp
      · have h1 : ¬ x ≤ 0 := by omega
        have h2 : ¬ x < 0 := by omega
        have h3 : ¬ x < (0 : ℕ) + 1 := by omega
        simp only [h1, h2, h3, ite_false]
        rfl
  | succ m ih =>
      rw [approx_succ, factF_st]
      by_cases hx : x ≤ 0
      · by_cases hx' : x < 0
        · simp [hx, hx']
        · obtain rfl : x = 0 := by omega
          have h : (0 : ℤ) < (m + 1 : ℕ) + 1 := by omega
          push_cast at h
          simp [h]
      · obtain ⟨k, rfl⟩ : ∃ k : ℕ, x = k + 1 := ⟨(x - 1).toNat, by omega⟩
        have h1 : (k : ℤ) + 1 - 1 = k := by omega
        have h2 : ¬ (k : ℤ) < 0 := by omega
        have h3 : ¬ (k : ℤ) + 1 < 0 := by omega
        have h4 : (k : ℤ) < m + 1 ↔ (k : ℤ) + 1 < (m + 1 : ℕ) + 1 := by push_cast; omega
        have h5 : ((k : ℤ) + 1).toNat = k + 1 := by omega
        simp only [hx, ↓reduceIte]
        rw [h1, ih]
        simp only [h2, h3, ite_false, h4, h5, Int.toNat_natCast, Nat.factorial_succ]
        congr 3
        push_cast
        ring

/-- §1.2 (p. 10): the closed form of the Kleene approximations,
`wₙ[X ↦ x, Y ↦ y] = [X ↦ x, Y ↦ y]` if `x < 0`, `[X ↦ 0, Y ↦ x! ⋅ y]` if
`0 ≤ x < n`, and undefined if `x ≥ n`. The notes state this for all `n`; it
holds for `n ≥ 1`, since `w₀ = ⊥` (`approx_zero`). -/
theorem approx_st {n : ℕ} (hn : 0 < n) (x y : ℤ) :
    approx n (st x y) =
      if x < 0 then Part.some (st x y)
      else if x < n then Part.some (st 0 (x.toNat.factorial * y))
      else Part.none := by
  obtain ⟨m, rfl⟩ := Nat.exists_eq_succ_of_ne_zero hn.ne'
  rw [approx_succ_st]
  push_cast
  rfl

/-- The closed form of `w∞` (p. 10), as a total function on states. -/
def wInf (s : State Loc) : State Loc :=
  if s X < 0 then s else st 0 ((s X).toNat.factorial * s Y)

/-- §1.2 (p. 10): the least fixed point `w∞ = ⨆ₙ wₙ` is
`w∞[X ↦ x, Y ↦ y] = [X ↦ x, Y ↦ y]` if `x < 0` and `[X ↦ 0, Y ↦ x! ⋅ y]` if
`x ≥ 0`. -/
theorem fix_factF_st (x y : ℤ) :
    fix factF (st x y) = Part.some (wInf (st x y)) := by
  refine Part.ext fun t => ?_
  rw [mem_fix_iff, Part.mem_some_iff, wInf]
  simp only [st_X, st_Y]
  constructor
  · rintro ⟨n, hn⟩
    cases n with
    | zero => exact absurd hn (Part.notMem_none t)
    | succ m =>
        change t ∈ approx (m + 1) (st x y) at hn
        rw [approx_succ_st] at hn
        by_cases hx : x < 0
        · simpa [hx] using hn
        · by_cases hm : x < m + 1
          · simpa [hx, hm] using hn
          · simp [hx, hm] at hn
  · rintro rfl
    refine ⟨x.toNat + 1, ?_⟩
    change _ ∈ approx (x.toNat + 1) (st x y)
    rw [approx_succ_st]
    have hlt : x < max x 0 + 1 := by omega
    by_cases hx : x < 0 <;> simp [hx, hlt]

/-- §1.2 and Example 13: the denotation of program (3) is `w∞`. -/
theorem denote_factorialProg_eq : C⟦factorialProg⟧ = fun s => Part.some (wInf s) := by
  funext s
  rw [eq_st s, denote_factorialProg, fix_factF_st]

/-- §1.2: `w∞` is what the operational semantics computes for program (3). -/
theorem exec_factorialProg_iff {s s' : State Loc} : Exec factorialProg s s' ↔ s' = wInf s := by
  rw [exec_iff_mem_denote, denote_factorialProg_eq, Part.mem_some_iff]

section Example16

/-- Example 16: for fixed `x` and `y`, the set of `w` with
`(w[X ↦ x, Y ↦ y] ⇓) ⇒ w[X ↦ x, Y ↦ y](Y) ≥ 0`. -/
def PostNonneg (x y : ℤ) : Set (State Loc → Part (State Loc)) :=
  {w | ∀ t ∈ w (st x y), 0 ≤ t Y}

/-- Example 16: the set `S = ⋂_{x, y ≥ 0} PostNonneg x y`. -/
def nonnegInvariant : Set (State Loc → Part (State Loc)) :=
  ⋂ (x : ℤ) (y : ℤ) (_ : 0 ≤ y), PostNonneg x y

/-- Example 16: each `PostNonneg x y` is chain-closed. Along a chain, either
every `wᵢ[X ↦ x, Y ↦ y]` is undefined, and so is the lub, or some
`wᵢ[X ↦ x, Y ↦ y]` is defined, and then the chain is constant from `i` on at
this state, so the lub takes the same value. -/
theorem chainClosed_postNonneg (x y : ℤ) : ChainClosed (PostNonneg x y) := by
  intro c hc t ht
  by_cases hdef : ∃ n, (c n (st x y)).Dom
  · obtain ⟨n, hn⟩ := hdef
    have hconst : Chain.EventuallyConstantFrom (c.map (Pi.evalOrderHom (st x y))) n :=
      fun m hm => ((Flat.le_iff_eq_none_or_eq.1 (c.monotone hm (st x y))).resolve_left
        fun h => Part.eq_none_iff'.1 h hn).symm
    have hlub : ωSup c (st x y) = c n (st x y) := Chain.ωSup_eq_of_eventuallyConstant hconst
    rw [hlub] at ht
    exact hc n t ht
  · obtain ⟨n, hn⟩ := partialFun_mem_ωSup.1 ht
    exact absurd ⟨n, Part.dom_iff_mem.2 ⟨t, hn⟩⟩ hdef

/-- Example 16: `S` is chain-closed, as an intersection of chain-closed sets
(Proposition 20). -/
theorem chainClosed_nonnegInvariant : ChainClosed nonnegInvariant :=
  ChainClosed.iInter fun x => ChainClosed.iInter fun y =>
    ChainClosed.iInter fun _ => chainClosed_postNonneg x y

/-- Example 16: `S` is admissible, since the nowhere-defined function lies in
it. -/
theorem admissible_nonnegInvariant : Admissible nonnegInvariant := by
  refine ⟨?_, chainClosed_nonnegInvariant⟩
  simp only [nonnegInvariant, Set.mem_iInter]
  intro x y _ t ht
  exact absurd ht (Part.notMem_none t)

/-- Example 16: `S` is stable under `F`. -/
theorem factF_mem_nonnegInvariant {w : State Loc → Part (State Loc)} (hw : w ∈ nonnegInvariant) :
    factF w ∈ nonnegInvariant := by
  simp only [nonnegInvariant, Set.mem_iInter] at hw ⊢
  intro x y hy t ht
  rw [factF_st] at ht
  split_ifs at ht with hx
  · rw [Part.mem_some_iff.1 ht]
    exact hy
  · exact hw (x - 1) (x * y) (Int.mul_nonneg (not_le.1 hx).le hy) t ht

/-- Example 16 (partial correctness by Scott induction): for all `x` and all
`y ≥ 0`, if `fix(F)[X ↦ x, Y ↦ y]` is defined then its `Y` component is
non-negative. -/
theorem fix_factF_Y_nonneg (x y : ℤ) (hy : 0 ≤ y) (h : (fix factF (st x y)).Dom) :
    0 ≤ (fix factF (st x y)).get h Y := by
  have hS : fix factF ∈ nonnegInvariant :=
    fix_mem admissible_nonnegInvariant fun _ => factF_mem_nonnegInvariant
  simp only [nonnegInvariant, Set.mem_iInter] at hS
  exact hS x y hy _ (Part.get_mem h)

end Example16

end Silmaril.Calculi.Imp.Factorial
