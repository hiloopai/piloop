module

public import Silmaril.Semantics.Domain.Basic
public import Silmaril.Semantics.Domain.Chain
public import Silmaril.Semantics.Domain.Flat
public import Silmaril.Semantics.Domain.Function
public import Silmaril.Semantics.Domain.BoundedComplete
public import Silmaril.Semantics.Domain.Finite
public import Mathlib.Basic.Finite.Prod
public import Mathlib.Data.Fintype.Option

/-! # Finite projections

A *finite projection sequence* on an ω-cpo `D` (`FiniteProjections D`) is an
increasing sequence of continuous idempotent deflations `proj n : D →𝒄 D`, each
with finitely many fixed points, whose supremum is the identity. This is the
SFP (bifinite) skeleton which we use in place of compact elements: every
element is the ω-lub of its approximations `proj n x` (`ωSup_chainAt`), and
each approximation ranges over a finite set.

The flat natural numbers carry the truncations `natTrunc n` (`nat`), every
finite ω-cpo carries the constant identity sequence (`ofFinite`), and finite
projection sequences on `D` and `E` induce one on `D →𝒄 E` by conjugation
(`arrow`): `(P.arrow Q).proj n f = Q.proj n ∘ f ∘ P.proj n`. Fixed points of the
conjugation are determined by their values on the fixed points of `P.proj n`
(`le_proj_iff_of_fixed`, `bddAbove_proj_iff_of_fixed`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

/-- An increasing sequence of idempotent deflations with finitely many fixed points and
supremum the identity. -/
structure FiniteProjections (D : Type*) [OmegaCompletePartialOrder D] where
  /-- The `n`-th projection. -/
  proj : ℕ → D →𝒄 D
  /-- The projections increase. -/
  monotone_proj : Monotone proj
  /-- Each projection is deflationary. -/
  proj_le : ∀ n x, proj n x ≤ x
  /-- Each projection is idempotent. -/
  proj_proj : ∀ n x, proj n (proj n x) = proj n x
  /-- The projections approximate every element. -/
  ωSup_proj : ∀ x, ωSup (⟨fun n => proj n x, fun _ _ h => monotone_proj h x⟩ : Chain D) = x
  /-- Each projection has finitely many fixed points. -/
  finite_fixed : ∀ n, {x | proj n x = x}.Finite

namespace FiniteProjections

variable {D E : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-- The chain of approximations `n ↦ proj n x` of an element. -/
def chainAt (P : FiniteProjections D) (x : D) : Chain D :=
  ⟨fun n => P.proj n x, fun _ _ h => P.monotone_proj h x⟩

@[simp] theorem chainAt_apply (P : FiniteProjections D) (x : D) (n : ℕ) :
    P.chainAt x n = P.proj n x := rfl

/-- Every element is the lub of its approximations. -/
theorem ωSup_chainAt (P : FiniteProjections D) (x : D) : ωSup (P.chainAt x) = x :=
  P.ωSup_proj x

/-- Projections are monotone in the index, pointwise. -/
theorem proj_le_proj (P : FiniteProjections D) {n m : ℕ} (h : n ≤ m) (x : D) :
    P.proj n x ≤ P.proj m x :=
  P.monotone_proj h x

/-! ## Flat natural numbers -/

/-- Flat truncation of ℕ⊥ to values below n. -/
noncomputable def natTrunc (n : ℕ) : Part ℕ →𝒄 Part ℕ :=
  Flat.lift fun k => if k < n then Part.some k else Part.none

@[simp] theorem natTrunc_some (n k : ℕ) :
    natTrunc n (Part.some k) = if k < n then Part.some k else Part.none :=
  Flat.lift_some _ k

@[simp] theorem natTrunc_none (n : ℕ) : natTrunc n Part.none = Part.none :=
  Flat.lift_none _

/-- The truncations increase with the bound. -/
theorem monotone_natTrunc : Monotone natTrunc := by
  intro n m h x
  show natTrunc n x ≤ natTrunc m x
  rcases Part.eq_none_or_eq_some x with rfl | ⟨k, rfl⟩
  · simp
  · simp only [natTrunc_some]
    by_cases hk : k < n
    · rw [ite_eq_left hk, ite_eq_left (lt_of_lt_of_le hk h)]
    · rw [ite_eq_right hk]; exact Flat.none_le _

/-- The truncations of the flat natural numbers. -/
noncomputable def nat : FiniteProjections (Part ℕ) where
  proj := natTrunc
  monotone_proj := monotone_natTrunc
  proj_le n x := by
    show natTrunc n x ≤ x
    rcases Part.eq_none_or_eq_some x with rfl | ⟨k, rfl⟩
    · simp
    · simp only [natTrunc_some]
      split_ifs
      · exact le_rfl
      · exact Flat.none_le _
  proj_proj n x := by
    show natTrunc n (natTrunc n x) = natTrunc n x
    rcases Part.eq_none_or_eq_some x with rfl | ⟨k, rfl⟩
    · simp
    · by_cases hk : k < n <;> simp [hk]
  ωSup_proj x := by
    rcases Part.eq_none_or_eq_some x with rfl | ⟨k, rfl⟩
    · exact le_antisymm (ωSup_le _ _ fun n => le_of_eq (natTrunc_none n)) (Flat.none_le _)
    · have h : Chain.EventuallyConstantFrom
          (⟨fun n => natTrunc n (Part.some k), fun _ _ h => monotone_natTrunc h _⟩ :
            Chain (Part ℕ)) (k + 1) := by
        intro n hn
        change natTrunc n (Part.some k) = natTrunc (k + 1) (Part.some k)
        rw [natTrunc_some, natTrunc_some, ite_eq_left (show k < n by omega),
          ite_eq_left k.lt_succ_self]
      refine (Chain.ωSup_eq_of_eventuallyConstant h).trans ?_
      change natTrunc (k + 1) (Part.some k) = Part.some k
      rw [natTrunc_some, ite_eq_left k.lt_succ_self]
  finite_fixed n := by
    refine (((Set.finite_Iio n).image Part.some).insert Part.none).subset ?_
    intro x hx
    rcases Part.eq_none_or_eq_some x with rfl | ⟨k, rfl⟩
    · exact Set.mem_insert _ _
    · refine Set.mem_insert_of_mem _ ?_
      have hk : k < n := by
        by_contra hk
        have : natTrunc n (Part.some k) = Part.some k := hx
        simp [hk] at this
      simpa using hk

/-! ## Finite domains -/

/-- A finite ω-cpo carries the constant identity projections. -/
def ofFinite [Finite D] : FiniteProjections D where
  proj _ := ContinuousHom.id
  monotone_proj _ _ _ := le_rfl
  proj_le _ _ := le_rfl
  proj_proj _ _ := rfl
  ωSup_proj x := le_antisymm (ωSup_le _ _ fun _ => le_rfl) (le_ωSup (c := ⟨fun _ => x,
    fun _ _ _ => le_rfl⟩) 0)
  finite_fixed _ := Set.toFinite _

/-! ## Function spaces -/

/-- Conjugation by projections, `f ↦ q ∘ f ∘ p`, as a continuous map on continuous
functions: two instances of the continuous composition `compHom`. -/
def conj (p : D →𝒄 D) (q : E →𝒄 E) : (D →𝒄 E) →𝒄 (D →𝒄 E) :=
  compHom.comp (pairHom (ContinuousHom.const q)
    (compHom.comp (pairHom ContinuousHom.id (ContinuousHom.const p))))

@[simp] theorem conj_apply (p : D →𝒄 D) (q : E →𝒄 E) (f : D →𝒄 E) (x : D) :
    conj p q f x = q (f (p x)) := rfl

/-- A fixed point of conjugation by an idempotent `p` factors through `p`. -/
theorem conj_fixed_apply {p : D →𝒄 D} {q : E →𝒄 E} (hp : ∀ x, p (p x) = p x)
    {e : D →𝒄 E} (he : conj p q e = e) (x : D) : e (p x) = e x := by
  rw [← he, conj_apply, conj_apply, hp]

/-- A fixed point of conjugation by an idempotent `q` takes values fixed by `q`. -/
theorem conj_fixed_proj {p : D →𝒄 D} {q : E →𝒄 E} (hq : ∀ y, q (q y) = q y)
    {e : D →𝒄 E} (he : conj p q e = e) (x : D) : q (e x) = e x := by
  conv_lhs => rw [← he]
  rw [conj_apply, hq, ← conj_apply p q, he]

/-- Finite projections on `D` and `E` induce finite projections on `D →𝒄 E`, by
conjugation. -/
noncomputable def arrow (P : FiniteProjections D) (Q : FiniteProjections E) :
    FiniteProjections (D →𝒄 E) where
  proj n := conj (P.proj n) (Q.proj n)
  monotone_proj n m h f x :=
    (Q.proj_le_proj h _).trans ((Q.proj m).monotone (f.monotone (P.proj_le_proj h x)))
  proj_le n f x := (Q.proj_le n _).trans (f.monotone (P.proj_le n x))
  proj_proj n f := by
    ext x
    simp only [conj_apply, P.proj_proj, Q.proj_proj]
  ωSup_proj f := by
    ext x
    rw [continuousHom_ωSup_apply]
    refine le_antisymm (ωSup_le _ _ fun n =>
      (Q.proj_le n _).trans (f.monotone (P.proj_le n x))) ?_
    conv_lhs => rw [← P.ωSup_chainAt x, f.continuous]
    refine ωSup_le _ _ fun m => ?_
    change f (P.proj m x) ≤ _
    conv_lhs => rw [← Q.ωSup_chainAt (f (P.proj m x))]
    refine ωSup_le _ _ fun k => ?_
    refine le_trans ?_ (le_ωSup _ (max k m))
    change Q.proj k (f (P.proj m x)) ≤ Q.proj (max k m) (f (P.proj (max k m) x))
    exact (Q.proj_le_proj (le_max_left k m) _).trans
      ((Q.proj _).monotone (f.monotone (P.proj_le_proj (le_max_right k m) x)))
  finite_fixed n := by
    have : Finite {d // P.proj n d = d} := (P.finite_fixed n).to_subtype
    have : Finite {y // Q.proj n y = y} := (Q.finite_fixed n).to_subtype
    let F : {e : D →𝒄 E // conj (P.proj n) (Q.proj n) e = e} →
        ({d // P.proj n d = d} → {y // Q.proj n y = y}) := fun e a =>
      ⟨e.1 a.1, conj_fixed_proj (Q.proj_proj n) e.2 a.1⟩
    have hF : Function.Injective F := by
      intro e e' h
      apply Subtype.ext
      ext x
      rw [← conj_fixed_apply (P.proj_proj n) e.2 x,
        ← conj_fixed_apply (P.proj_proj n) e'.2 x]
      exact congrArg Subtype.val (congrFun h ⟨P.proj n x, P.proj_proj n x⟩)
    exact Set.finite_coe_iff.1 (Finite.of_injective F hF)

variable (P : FiniteProjections D) (Q : FiniteProjections E)

@[simp] theorem arrow_proj_apply (n : ℕ) (f : D →𝒄 E) (x : D) :
    (P.arrow Q).proj n f x = Q.proj n (f (P.proj n x)) := rfl

variable {P Q} {n : ℕ} {e : D →𝒄 E}

/-- A fixed point of the conjugation factors through `P.proj n`. -/
theorem apply_proj_of_fixed (he : (P.arrow Q).proj n e = e) (x : D) :
    e (P.proj n x) = e x :=
  conj_fixed_apply (P.proj_proj n) he x

/-- A fixed point of the conjugation takes values fixed by `Q.proj n`. -/
theorem proj_apply_of_fixed (he : (P.arrow Q).proj n e = e) (x : D) :
    Q.proj n (e x) = e x :=
  conj_fixed_proj (Q.proj_proj n) he x

/-- Below a projection, a fixed point of the conjugation is determined by its values
at fixed points of `P.proj n`. -/
theorem le_proj_iff_of_fixed (he : (P.arrow Q).proj n e = e) {x : D →𝒄 E} :
    e ≤ (P.arrow Q).proj n x ↔ ∀ d, P.proj n d = d → e d ≤ Q.proj n (x d) := by
  constructor
  · intro h d hd
    have h' : e d ≤ (P.arrow Q).proj n x d := h d
    rwa [arrow_proj_apply, hd] at h'
  · intro h y
    show e y ≤ (P.arrow Q).proj n x y
    rw [← apply_proj_of_fixed he y, arrow_proj_apply]
    exact h _ (P.proj_proj n y)

/-- Compatibility of a fixed point of the conjugation with a projection is tested at
fixed points of `P.proj n`. -/
theorem bddAbove_proj_iff_of_fixed (hE : BoundedComplete E)
    (he : (P.arrow Q).proj n e = e) {x : D →𝒄 E} :
    BddAbove ({e, (P.arrow Q).proj n x} : Set (D →𝒄 E)) ↔
      ∀ d, P.proj n d = d → BddAbove ({e d, Q.proj n (x d)} : Set E) := by
  rw [ContinuousHom.bddAbove_pair_iff hE]
  constructor
  · intro h d hd
    have h' := h d
    rwa [arrow_proj_apply, hd] at h'
  · intro h y
    rw [← apply_proj_of_fixed he y, arrow_proj_apply]
    exact h _ (P.proj_proj n y)

end FiniteProjections

end Silmaril.Semantics.Domain
