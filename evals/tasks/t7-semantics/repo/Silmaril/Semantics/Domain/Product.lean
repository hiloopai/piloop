module

public import Silmaril.Semantics.Domain.Continuity

/-! # Products of domains

Definitions 13–14 of the notes (§3.2) are Mathlib's product and Π-type
ω-cpos, with componentwise order, lubs and least elements (Propositions 8
and 12). This module proves Proposition 9 (a map out of a binary product is
monotone or continuous iff it is so in each argument separately), bundles the
projections and pairings of Propositions 10 and 12, and identifies binary
products with `Bool`-indexed ones (Remark 3).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D E F : Type*}

section Binary

variable [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]
  [OmegaCompletePartialOrder F]

/-- Proposition 8: lubs in a product are computed componentwise (Mathlib
`Prod.ωSup_fst`). -/
alias prod_ωSup_fst := Prod.ωSup_fst

/-- Proposition 8: lubs in a product are computed componentwise (Mathlib
`Prod.ωSup_snd`). -/
alias prod_ωSup_snd := Prod.ωSup_snd

/-- Proposition 8: the least element of a product is the pair of least elements. -/
theorem prod_bot [OrderBot D] [OrderBot E] : (⊥ : D × E) = (⊥, ⊥) := rfl

/-- Proposition 9: monotonicity in each argument separately. This is Mathlib's
`_root_.monotone_prod_iff` with its conjuncts swapped, kept for compatibility. Inside
`Silmaril.Semantics.Domain` this name shadows Mathlib's, so write `_root_.monotone_prod_iff`. -/
@[deprecated _root_.monotone_prod_iff +typeChanged (since := "2026-09-29")]
theorem monotone_prod_iff {f : D × E → F} :
    Monotone f ↔ (∀ e, Monotone fun d => f (d, e)) ∧ ∀ d, Monotone fun e => f (d, e) :=
  _root_.monotone_prod_iff.trans and_comm

/-- Proposition 9: continuity in each argument separately. Separate
monotonicity is Mathlib's `_root_.monotone_prod_iff`. Since `ωScottContinuous f`
is `ScottContinuousOn (Set.range fun c : Chain α => Set.range c) f`, the (←)
direction is the chain-range instance of Mathlib's `ScottContinuousOn.fromProd`
(`Mathlib/Order/ScottContinuity/Prod.lean`); we give a direct proof taking the
maximum of the two indices. -/
theorem ωScottContinuous_prod_iff {f : D × E → F} :
    ωScottContinuous f ↔
      (∀ e, ωScottContinuous fun d => f (d, e)) ∧ ∀ d, ωScottContinuous fun e => f (d, e) := by
  constructor
  · intro h
    exact ⟨fun _ => h.comp (Prod.ωScottContinuous.prodMk ωScottContinuous.id
        ωScottContinuous.const),
      fun _ => h.comp (Prod.ωScottContinuous.prodMk ωScottContinuous.const
        ωScottContinuous.id)⟩
  · rintro ⟨h₁, h₂⟩
    have hf : Monotone f :=
      _root_.monotone_prod_iff.2 ⟨fun d => (h₂ d).monotone, fun e => (h₁ e).monotone⟩
    refine ωScottContinuous_of_le hf fun c => ?_
    change f ((ωSup c).1, (ωSup c).2) ≤ _
    rw [prod_ωSup_fst, (h₁ _).map_ωSup]
    refine ωSup_le _ _ fun i => ?_
    change f ((c i).1, (ωSup c).2) ≤ _
    rw [prod_ωSup_snd, (h₂ _).map_ωSup]
    refine ωSup_le _ _ fun j => ?_
    change f ((c i).1, (c j).2) ≤ _
    have hij : ((c i).1, (c j).2) ≤ c (max i j) :=
      ⟨(c.monotone (le_max_left i j)).1, (c.monotone (le_max_right i j)).2⟩
    exact (hf hij).trans
      (le_ωSup (c.map ⟨f, hf⟩) (max i j))

/-- Proposition 10: the first projection. -/
def fstHom : D × E →𝒄 D := ContinuousHom.ofFun Prod.fst Prod.ωScottContinuous_fst

/-- Proposition 10: the second projection. -/
def sndHom : D × E →𝒄 E := ContinuousHom.ofFun Prod.snd Prod.ωScottContinuous_snd

/-- Proposition 10: pairing of continuous maps. -/
def pairHom (f : D →𝒄 E) (g : D →𝒄 F) : D →𝒄 E × F :=
  ContinuousHom.ofFun (fun d => (f d, g d))
    (Prod.ωScottContinuous.prodMk f.ωScottContinuous g.ωScottContinuous)

@[simp] theorem fstHom_apply (p : D × E) : fstHom p = p.1 := rfl
@[simp] theorem sndHom_apply (p : D × E) : sndHom p = p.2 := rfl
@[simp] theorem pairHom_apply (f : D →𝒄 E) (g : D →𝒄 F) (d : D) : pairHom f g d = (f d, g d) :=
  rfl

end Binary

section General

variable {I : Type*} {P : I → Type*} [∀ i, OmegaCompletePartialOrder (P i)]
  [OmegaCompletePartialOrder D]

/-- Proposition 12: lubs in a general product are computed componentwise. -/
theorem pi_ωSup_apply (c : Chain (∀ i, P i)) (i : I) :
    ωSup c i = ωSup (c.map (Pi.evalOrderHom i)) := rfl

/-- Proposition 12: the least element of a general product is pointwise
(Mathlib `Pi.bot_apply`). -/
alias pi_bot_apply := Pi.bot_apply

/-- Proposition 12: the `i`th projection. -/
def evalHom (i : I) : (∀ i, P i) →𝒄 P i :=
  ContinuousHom.ofFun (fun p => p i) (ωScottContinuous.apply i)

/-- Tupling of a family of continuous maps into a general product. -/
def piHom (f : ∀ i, D →𝒄 P i) : D →𝒄 ∀ i, P i :=
  ContinuousHom.ofFun (fun d i => f i d)
    (ωScottContinuous.of_apply₂ fun i => (f i).ωScottContinuous)

@[simp] theorem evalHom_apply (i : I) (p : ∀ i, P i) : evalHom i p = p i := rfl
@[simp] theorem piHom_apply (f : ∀ i, D →𝒄 P i) (d : D) (i : I) : piHom f d i = f i d := rfl

end General

section Remark3

variable {P : Bool → Type*} [∀ b, OmegaCompletePartialOrder (P b)]

/-- Remark 3: a `Bool`-indexed product is a binary product. -/
def piBoolOrderIso : (∀ b, P b) ≃o P true × P false where
  toFun p := (p true, p false)
  invFun q b := match b with
    | true => q.1
    | false => q.2
  left_inv p := funext fun b => by cases b <;> rfl
  right_inv _ := rfl
  map_rel_iff' {p q} := by
    change (p true, p false) ≤ (q true, q false) ↔ p ≤ q
    rw [Prod.mk_le_mk]
    exact ⟨fun ⟨h₁, h₂⟩ b => by cases b <;> assumption, fun h => ⟨h true, h false⟩⟩

/-- Remark 3: the identification preserves lubs. -/
theorem piBoolOrderIso_ωSup (c : Chain (∀ b, P b)) :
    piBoolOrderIso (ωSup c) = ωSup (c.map piBoolOrderIso.toOrderEmbedding.toOrderHom) := rfl

end Remark3

end Silmaril.Semantics.Domain
