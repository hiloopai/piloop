module

public import Silmaril.Semantics.Domain.Examples
public import Silmaril.Semantics.Domain.Function

/-! # Continuous predicates on the vertical naturals

Exercise 6 of the notes: the function domain `Ω → {⊤}⊥` is in bijection with
`Ω = ℕ∞`. A continuous `f` is determined by where it becomes defined. That set
is upward closed and, by continuity, contains no infinite element unless it
contains a finite one. So it is either empty or `[n, ω]` for a finite `n`.
The bijection sends `f` to that threshold, with `ω` for the empty set; it
reverses the order.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

namespace OmegaThreshold

open Classical in
/-- The predicate that becomes defined from the finite threshold `n` on
(nowhere, when `n = ω`). -/
noncomputable def predicate (n : ℕ∞) (x : ℕ∞) : Part Unit :=
  if n ≠ ⊤ ∧ n ≤ x then Part.some () else Part.none

theorem predicate_dom {n x : ℕ∞} : (predicate n x).Dom ↔ n ≠ ⊤ ∧ n ≤ x := by
  unfold predicate
  split_ifs with h <;> simp [h]

/-- A finite element below a lub of naturals is below some element of the chain. -/
theorem exists_le_of_le_ωSup {m : ℕ} {c : Chain ℕ∞} (h : (m : ℕ∞) ≤ ωSup c) :
    ∃ k, (m : ℕ∞) ≤ c k := by
  cases m with
  | zero => exact ⟨0, by simp⟩
  | succ m =>
      have hlt : (m : ℕ∞) < ⨆ k, c k := by
        rw [← ENat.add_one_le_iff (ENat.natCast_ne_top m)]
        exact_mod_cast h
      obtain ⟨k, hk⟩ := lt_iSup_iff.1 hlt
      exact ⟨k, by exact_mod_cast (ENat.add_one_le_iff (ENat.natCast_ne_top m)).2 hk⟩

theorem predicate_continuous (n : ℕ∞) : ωScottContinuous (predicate n) := by
  have hmono : Monotone (predicate n) := fun x y hxy => by
    by_cases h : n ≠ ⊤ ∧ n ≤ x
    · have hy : n ≠ ⊤ ∧ n ≤ y := ⟨h.1, h.2.trans hxy⟩
      simp [predicate, h, hy]
    · simp only [predicate, h, ite_false]
      exact bot_le
  refine ωScottContinuous_of_le hmono fun c => ?_
  by_cases h : n ≠ ⊤ ∧ n ≤ ωSup c
  · obtain ⟨m, rfl⟩ := ENat.ne_top_iff_exists.1 h.1
    obtain ⟨k, hk⟩ := exists_le_of_le_ωSup h.2
    refine le_ωSup_of_le k ?_
    change predicate _ (ωSup c) ≤ predicate _ (c k)
    simp [predicate, h, hk]
  · simp only [predicate, h, ite_false]
    exact bot_le

/-- The continuous predicate with threshold `n`. -/
noncomputable def toFun (n : ℕ∞) : ℕ∞ →𝒄 Part Unit :=
  ContinuousHom.ofFun (predicate n) (predicate_continuous n)

/-- The threshold of a continuous predicate: the least point where it is
defined. -/
noncomputable def threshold (f : ℕ∞ →𝒄 Part Unit) : ℕ∞ := sInf {x | (f x).Dom}

theorem dom_mono (f : ℕ∞ →𝒄 Part Unit) {x y : ℕ∞} (hxy : x ≤ y) (hx : (f x).Dom) :
    (f y).Dom :=
  (Part.dom_iff_mem.1 hx).elim fun a ha => Part.dom_iff_mem.2 ⟨a, f.monotone hxy a ha⟩

/-- A continuous predicate defined at `ω` is defined at some finite point. -/
theorem exists_nat_dom (f : ℕ∞ →𝒄 Part Unit) (h : (f ⊤).Dom) : ∃ n : ℕ, (f n).Dom := by
  rw [← ωSup_omegaChain, f.continuous, Part.dom_iff_mem] at h
  obtain ⟨a, ha⟩ := h
  obtain ⟨n, hn⟩ := (Part.mem_ωSup a _).1 ha
  exact ⟨n, Part.dom_iff_mem.2 ⟨a, Part.eq_some_iff.1 hn.symm⟩⟩

theorem part_unit_eq {p q : Part Unit} (h : p.Dom ↔ q.Dom) : p = q :=
  Part.ext' h fun _ _ => rfl

theorem threshold_toFun (n : ℕ∞) : threshold (toFun n) = n := by
  unfold threshold
  by_cases hn : n = ⊤
  · subst hn
    have : {x : ℕ∞ | (toFun ⊤ x).Dom} = ∅ := by
      ext x
      simp [toFun, predicate_dom]
    rw [this, sInf_empty]
  · have : {x : ℕ∞ | (toFun n x).Dom} = Set.Ici n := by
      ext x
      simp [toFun, predicate_dom, hn]
    rw [this, csInf_Ici]

theorem toFun_threshold (f : ℕ∞ →𝒄 Part Unit) : toFun (threshold f) = f := by
  ext1 x
  apply part_unit_eq
  change (predicate _ x).Dom ↔ _
  rw [predicate_dom]
  by_cases hS : {x : ℕ∞ | (f x).Dom}.Nonempty
  · have hmem : threshold f ∈ {x : ℕ∞ | (f x).Dom} := csInf_mem hS
    have hfin : threshold f ≠ ⊤ := by
      intro htop
      have htop' : (f ⊤).Dom := htop ▸ hmem
      obtain ⟨n, hn⟩ := exists_nat_dom f htop'
      have : threshold f ≤ n := csInf_le (OrderBot.bddBelow _) hn
      rw [htop] at this
      exact ENat.natCast_ne_top n (top_le_iff.1 this)
    constructor
    · exact fun ⟨_, hx⟩ => dom_mono f hx hmem
    · exact fun hx => ⟨hfin, csInf_le (OrderBot.bddBelow _) hx⟩
  · have hx : ¬ (f x).Dom := fun hx => hS ⟨x, hx⟩
    have htop : threshold f = ⊤ := by
      rw [threshold, Set.not_nonempty_iff_eq_empty.1 hS, sInf_empty]
    simp [htop, hx]

/-- Exercise 6: `Ω → {⊤}⊥` is in bijection with `Ω`. -/
noncomputable def equiv : (ℕ∞ →𝒄 Part Unit) ≃ ℕ∞ where
  toFun := threshold
  invFun := toFun
  left_inv := toFun_threshold
  right_inv := threshold_toFun

/-- The bijection of Exercise 6 reverses the order. -/
theorem threshold_anti {f g : ℕ∞ →𝒄 Part Unit} (h : f ≤ g) : threshold g ≤ threshold f :=
  sInf_le_sInf fun x hx => Part.dom_iff_mem.2 ((Part.dom_iff_mem.1 hx).elim
    fun a ha => ⟨a, h x a ha⟩)

end OmegaThreshold

end Silmaril.Semantics.Domain
