module

public import Silmaril.Semantics.Domain.Omega
public import Mathlib.Order.DirSupClosed

/-! # Scott induction

Section 4 of the notes. A subset of a cpo is *chain-closed* if it contains the
lubs of its chains, and *admissible* if it also contains `⊥`. Scott induction
(Theorem 17) proves that `fix f` lies in every admissible subset stable under
`f`. Propositions 18–21 build chain-closed sets from inequalities and
equalities between continuous functions, closing under inverse images, finite
unions, arbitrary intersections and universal quantification.
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D E : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- A subset containing the lub of each of its chains. -/
def ChainClosed (S : Set D) : Prop := ∀ c : Chain D, (∀ n, c n ∈ S) → ωSup c ∈ S

/-- Remark 4: an admissible subset is chain-closed and contains `⊥`. This is
unrelated to Mathlib's Bourbaki–Witt notion `ChainCompletePartialOrder.IsAdmissible`. -/
def Admissible [OrderBot D] (S : Set D) : Prop := ⊥ ∈ S ∧ ChainClosed S

/-- Theorem 17 (Scott induction). -/
theorem fix_mem [OrderBot D] {f : D →𝒄 D} {S : Set D} (hS : Admissible S)
    (hf : ∀ x ∈ S, f x ∈ S) : fix f ∈ S := by
  refine hS.2 _ fun n => ?_
  induction n with
  | zero => exact hS.1
  | succ n ih =>
      rw [kleeneChain_succ]
      exact hf _ ih

/-- Scott induction phrased for a property. -/
theorem fix_induction [OrderBot D] {f : D →𝒄 D} {P : D → Prop} (hbot : P ⊥)
    (hclosed : ChainClosed {x | P x}) (hstep : ∀ x, P x → P (f x)) : P (fix f) :=
  fix_mem (S := {x | P x}) ⟨hbot, hclosed⟩ hstep

namespace ChainClosed

theorem univ : ChainClosed (Set.univ : Set D) := fun _ _ => trivial

theorem empty : ChainClosed (∅ : Set D) := fun _ h => (h 0).elim

/-- Proposition 18: `{(x, y) | x ⊑ y}` is chain-closed. -/
theorem le : ChainClosed {p : D × D | p.1 ≤ p.2} := fun c h =>
  ωSup_le _ _ fun n => (h n).trans (le_ωSup (c.map OrderHom.snd) n)

/-- Proposition 18: `{(x, y) | x = y}` is chain-closed. -/
theorem eq : ChainClosed {p : D × D | p.1 = p.2} := fun c h => by
  change ωSup (c.map OrderHom.fst) = ωSup (c.map OrderHom.snd)
  congr 1
  ext n
  exact h n

/-- Proposition 19: inverse images along continuous maps preserve
chain-closure. -/
theorem preimage {f : D → E} (hf : ωScottContinuous f) {S : Set E} (hS : ChainClosed S) :
    ChainClosed (f ⁻¹' S) := fun c h => by
  change f (ωSup c) ∈ S
  rw [hf.map_ωSup]
  exact hS _ h

/-- Proposition 20: binary intersections. -/
theorem inter {S T : Set D} (hS : ChainClosed S) (hT : ChainClosed T) :
    ChainClosed (S ∩ T) := fun c h => ⟨hS c fun n => (h n).1, hT c fun n => (h n).2⟩

/-- Proposition 20: arbitrary intersections. -/
theorem iInter {ι : Sort*} {S : ι → Set D} (hS : ∀ i, ChainClosed (S i)) :
    ChainClosed (⋂ i, S i) := fun c h =>
  Set.mem_iInter.2 fun i => hS i c fun n => Set.mem_iInter.1 (h n) i

/-- Proposition 20: binary unions. One of the two sets contains infinitely
many elements of the chain, and those elements form a cofinal subchain. -/
theorem union {S T : Set D} (hS : ChainClosed S) (hT : ChainClosed T) :
    ChainClosed (S ∪ T) := by
  intro c h
  -- If `S` contains elements of the chain at arbitrarily late indices, the lub
  -- of the chain is the lub of a subchain inside `S`; otherwise the same holds
  -- for `T`.
  have key : ∀ {U : Set D}, ChainClosed U → (∀ N, ∃ n, N ≤ n ∧ c n ∈ U) → ωSup c ∈ U := by
    intro U hU hfreq
    choose g hg using hfreq
    let idx : ℕ → ℕ := fun k => Nat.rec (g 0) (fun _ m => g (m + 1)) k
    have hmono : StrictMono idx := strictMono_nat_of_lt_succ fun k =>
      Nat.lt_of_lt_of_le (Nat.lt_succ_self _) (hg (idx k + 1)).1
    let c' : Chain D := ⟨⟨fun k => c (idx k), fun _ _ h => c.monotone (hmono.monotone h)⟩⟩
    have hsup : ωSup c' = ωSup c := by
      refine le_antisymm (ωSup_le _ _ fun k => le_ωSup c (idx k)) (ωSup_le _ _ fun n => ?_)
      exact (c.monotone (hmono.id_le n)).trans (le_ωSup c' n)
    rw [← hsup]
    refine hU c' fun k => ?_
    cases k with
    | zero => exact (hg 0).2
    | succ k => exact (hg (idx k + 1)).2
  by_cases hfreq : ∀ N, ∃ n, N ≤ n ∧ c n ∈ S
  · exact Or.inl (key hS hfreq)
  · simp only [not_forall, not_exists, not_and] at hfreq
    obtain ⟨N, hN⟩ := hfreq
    refine Or.inr (key hT fun M => ⟨max N M, le_max_right N M, ?_⟩)
    exact (h _).resolve_left (hN _ (le_max_left N M))

/-- Proposition 21: universal quantification over one component. -/
theorem forall_fst {P : D × E → Prop} (hP : ChainClosed {p | P p}) :
    ChainClosed {e | ∀ d, P (d, e)} := by
  have : {e | ∀ d, P (d, e)} = ⋂ d, (fun e => (d, e)) ⁻¹' {p | P p} := by
    ext e
    simp
  rw [this]
  exact iInter fun d =>
    hP.preimage (Prod.ωScottContinuous.prodMk ωScottContinuous.const ωScottContinuous.id)

/-- Inequalities between continuous functions determine chain-closed sets. -/
theorem le_of_continuous {F : Type*} [OmegaCompletePartialOrder F] {f g : D → F}
    (hf : ωScottContinuous f) (hg : ωScottContinuous g) : ChainClosed {x | f x ≤ g x} :=
  le.preimage (Prod.ωScottContinuous.prodMk hf hg)

/-- Equalities between continuous functions determine chain-closed sets. The
induced subtype ω-cpo (`ChainClosed.omegaCompletePartialOrder`) on this set is
Mathlib's `ωCPO.omegaCompletePartialOrderEqualizer`. -/
theorem eq_of_continuous {F : Type*} [OmegaCompletePartialOrder F] {f g : D → F}
    (hf : ωScottContinuous f) (hg : ωScottContinuous g) : ChainClosed {x | f x = g x} :=
  eq.preimage (Prod.ωScottContinuous.prodMk hf hg)

/-- Down-sets are chain-closed. -/
theorem Iic (d : D) : ChainClosed (Set.Iic d) := fun _ h => ωSup_le _ _ h

/-- In a cpo with no infinite strictly ascending chains, such as a flat domain,
every subset is chain-closed: the lub of a chain is one of its elements. -/
theorem of_wellFoundedGT [WellFoundedGT D] (S : Set D) : ChainClosed S := by
  intro c h
  obtain ⟨N, hN⟩ := Chain.eventuallyConstant_of_wellFoundedGT c
  rw [Chain.ωSup_eq_of_eventuallyConstant hN]
  exact h N

/-- Every finite subset is chain-closed. -/
theorem of_finite {S : Set D} (hS : S.Finite) : ChainClosed S := by
  intro c h
  obtain ⟨N, hconst⟩ :=
    Chain.eventuallyConstant_of_finite_range (hS.subset (Set.range_subset_iff.2 h))
  rw [Chain.ωSup_eq_of_eventuallyConstant hconst]
  exact h N

/-- In the domain of partial functions (Example 4), the partial functions
whose graph lies inside a fixed relation `R` form a chain-closed set, since the
graph of a lub is the union of the graphs. With Theorem 17 this proves that
every output of a recursively defined partial function satisfies `R`. -/
theorem graph_subset {X Y : Type*} (R : Set (X × Y)) :
    ChainClosed {w : X → Part Y | graph w ⊆ R} := by
  rintro c hc ⟨x, y⟩ hy
  obtain ⟨n, hn⟩ := partialFun_mem_ωSup.1 hy
  exact hc n (a := (x, y)) hn

/-- Chain-closure is the chain-indexed instance of Mathlib's closure under
directed sups, over the same family of sets `ωScottContinuous` is defined over. -/
theorem iff_dirSupClosedOn {S : Set D} :
    ChainClosed S ↔ DirSupClosedOn (Set.range fun c : Chain D => Set.range c) S := by
  constructor
  · rintro h _ ⟨c, rfl⟩ hsub - - a ha
    rw [IsLUB.unique ha (isLUB_range_ωSup c)]
    exact h c fun n => hsub ⟨n, rfl⟩
  · intro h c hc
    exact h ⟨c, rfl⟩ (Set.range_subset_iff.2 hc) (Set.range_nonempty _)
      (c.monotone.directed_le.directedOn_range) (isLUB_range_ωSup c)

/-- A chain-closed set is an ω-cpo with the lubs of the ambient ω-cpo. Not an
instance, since it depends on the proof of chain-closure. -/
abbrev omegaCompletePartialOrder {S : Set D} (h : ChainClosed S) :
    OmegaCompletePartialOrder S :=
  OmegaCompletePartialOrder.subtype (· ∈ S) fun c hc => h c fun n => hc _ ⟨n, rfl⟩

/-- The inclusion of a chain-closed set is continuous. -/
theorem ωScottContinuous_val {S : Set D} (h : ChainClosed S) :
    @ωScottContinuous S D h.omegaCompletePartialOrder _ Subtype.val :=
  @ωScottContinuous.of_monotone_map_ωSup S D h.omegaCompletePartialOrder _ _
    ⟨fun _ _ => id, fun _ => rfl⟩

end ChainClosed

/-- The combined example of §4.2: for continuous `fᵢ`, `g` and a constant
`e`, `Φ(x) := ∀ y, (∀ i, fᵢ(x) ⊑ y) ∨ g(x) = e` is chain-closed. -/
theorem chainClosed_forall_or_eq {ι : Type*} {f : ι → D → E} {g : D → E} {e : E}
    (hf : ∀ i, ωScottContinuous (f i)) (hg : ωScottContinuous g) :
    ChainClosed {x | ∀ y, (∀ i, f i x ≤ y) ∨ g x = e} := by
  have hpair : ChainClosed {p : E × D | (∀ i, f i p.2 ≤ p.1) ∨ g p.2 = e} := by
    refine ChainClosed.union (S := {p : E × D | ∀ i, f i p.2 ≤ p.1})
      (T := {p : E × D | g p.2 = e}) ?_ ?_
    · have : {p : E × D | ∀ i, f i p.2 ≤ p.1} = ⋂ i, {p : E × D | f i p.2 ≤ p.1} := by
        ext p
        simp
      rw [this]
      exact ChainClosed.iInter fun i =>
        ChainClosed.le_of_continuous ((hf i).comp Prod.ωScottContinuous_snd)
          Prod.ωScottContinuous_fst
    · exact ChainClosed.eq_of_continuous (hg.comp Prod.ωScottContinuous_snd)
        ωScottContinuous.const
  exact ChainClosed.forall_fst hpair

section Examples

/-- Example 14: every finite subset of `Ω` is chain-closed. -/
theorem omega_finite_chainClosed {S : Set ℕ∞} (hS : S.Finite) : ChainClosed S :=
  ChainClosed.of_finite hS

/-- The even natural numbers inside `Ω`. -/
def omegaEvens : Set ℕ∞ := {x | ∃ n : ℕ, x = ((2 * n : ℕ) : ℕ∞)}

/-- The chain `0 ⊑ 2 ⊑ 4 ⊑ …` in `Ω`. -/
noncomputable def omegaEvenChain : Chain ℕ∞ :=
  ⟨⟨fun n => ((2 * n : ℕ) : ℕ∞), fun _ _ h => ENat.natCast_le_natCast.2 (by omega)⟩⟩

theorem ωSup_omegaEvenChain : ωSup omegaEvenChain = ⊤ := by
  refine top_le_iff.1 ?_
  rw [← ωSup_omegaChain]
  exact ωSup_le _ _ fun n =>
    le_ωSup_of_le n (ENat.natCast_le_natCast.2 (by omega))

/-- Example 14: the even numbers are not chain-closed in `Ω`. -/
theorem omegaEvens_not_chainClosed : ¬ ChainClosed omegaEvens := by
  intro h
  obtain ⟨n, hn⟩ := h omegaEvenChain fun n => ⟨n, rfl⟩
  rw [ωSup_omegaEvenChain] at hn
  exact ENat.natCast_ne_top _ hn.symm

/-- Example 14: the even numbers together with `ω` are admissible in `Ω`. -/
theorem omegaEvens_top_admissible : Admissible (insert ⊤ omegaEvens) := by
  refine ⟨Or.inr ⟨0, rfl⟩, fun c h => ?_⟩
  by_cases hb : ∃ N, Chain.EventuallyConstantFrom c N
  · obtain ⟨N, hN⟩ := hb
    rw [Chain.ωSup_eq_of_eventuallyConstant hN]
    exact h N
  · -- A chain that is not eventually constant is unbounded in `ℕ`.
    refine Or.inl (top_le_iff.1 ?_)
    rw [← ωSup_omegaChain]
    refine ωSup_le _ _ fun m => ?_
    by_contra hlt
    rw [not_le] at hlt
    -- All elements of `c` are below `m`, so `c` is eventually constant.
    have hbound : (Set.range c).Finite := by
      refine ((Set.finite_Iio (m : ℕ)).image (fun k : ℕ => (k : ℕ∞))).subset ?_
      rintro _ ⟨n, rfl⟩
      have hn : c n < m := lt_of_le_of_lt (le_ωSup c n) hlt
      obtain ⟨k, hk⟩ := ENat.ne_top_iff_exists.1 (ne_top_of_lt hn)
      rw [← hk] at hn
      exact ⟨k, ENat.natCast_lt_natCast.1 hn, hk⟩
    exact hb (Chain.eventuallyConstant_of_finite_range hbound)

/-- Example 15: Scott induction proves (lfp-least). -/
theorem fix_le_of_scott [OrderBot D] {f : D →𝒄 D} {d : D} (h : f d ≤ d) : fix f ≤ d :=
  fix_mem (S := Set.Iic d) ⟨bot_le, ChainClosed.Iic d⟩ fun _ hx => (f.monotone hx).trans h

/-- Example 17: if `f ∘ g ⊑ g ∘ f` and `f(⊥) ⊑ g(⊥)` then `fix f ⊑ fix g`. -/
theorem fix_le_fix_of_comm [OrderBot D] {f g : D →𝒄 D} (hcomm : ∀ x, f (g x) ≤ g (f x))
    (hbot : f ⊥ ≤ g ⊥) : fix f ≤ fix g := by
  have hΦ : f (fix g) ≤ g (fix g) := by
    refine fix_induction (P := fun x => f x ≤ g x) hbot
      (ChainClosed.le_of_continuous f.ωScottContinuous g.ωScottContinuous) fun x hx => ?_
    exact (hcomm x).trans (g.monotone hx)
  exact fix_le (hΦ.trans (fix_eq g).le)

end Examples

section Exercises

/-- Exercise 9: the image of a chain-closed set under a continuous map need not
be chain-closed. Here the inclusion `ℕ → Ω` is continuous out of the discrete
cpo, `univ` is chain-closed, and its image (the finite elements) is not. -/
theorem exists_image_not_chainClosed :
    ∃ f : Discrete ℕ → ℕ∞, ωScottContinuous f ∧ ChainClosed (Set.univ : Set (Discrete ℕ)) ∧
      ¬ ChainClosed (f '' Set.univ) := by
  refine ⟨fun n => ((Discrete.get n : ℕ) : ℕ∞), ?_, ChainClosed.univ, ?_⟩
  · refine ωScottContinuous_of_le (fun a b h => (Discrete.le_iff.1 h) ▸ le_rfl) fun c => ?_
    exact le_ωSup_of_le 0 le_rfl
  · intro h
    obtain ⟨n, -, hn⟩ := h omegaChain fun n => ⟨Discrete.of n, trivial, rfl⟩
    rw [ωSup_omegaChain] at hn
    exact ENat.natCast_ne_top _ hn

/-- The finite diagonal of `Ω × Ω`. -/
def finiteDiagonal : Set (ℕ∞ × ℕ∞) := {p | ∃ n : ℕ, p = ((n : ℕ∞), (n : ℕ∞))}

/-- Exercise 10: every slice of the finite diagonal is chain-closed. -/
theorem finiteDiagonal_slices :
    (∀ d, ChainClosed {d' | (d, d') ∈ finiteDiagonal}) ∧
      ∀ d', ChainClosed {d | (d, d') ∈ finiteDiagonal} := by
  refine ⟨fun d => ChainClosed.of_finite ((Set.finite_singleton d).subset ?_),
    fun d' => ChainClosed.of_finite ((Set.finite_singleton d').subset ?_)⟩
  · rintro _ ⟨n, hn⟩
    simp only [Prod.mk.injEq] at hn
    exact hn.2.trans hn.1.symm
  · rintro _ ⟨n, hn⟩
    simp only [Prod.mk.injEq] at hn
    exact hn.1.trans hn.2.symm

/-- Exercise 10: the finite diagonal itself is not chain-closed. -/
theorem finiteDiagonal_not_chainClosed : ¬ ChainClosed finiteDiagonal := by
  intro h
  obtain ⟨n, hn⟩ := h (omegaChain.zip omegaChain) fun n => ⟨n, rfl⟩
  have := congrArg Prod.fst hn
  change ωSup omegaChain = _ at this
  rw [ωSup_omegaChain] at this
  exact ENat.natCast_ne_top _ this.symm

end Exercises

end Silmaril.Semantics.Domain
