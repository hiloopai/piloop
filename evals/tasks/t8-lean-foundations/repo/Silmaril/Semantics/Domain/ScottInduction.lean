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
    (hf : ∀ x ∈ S, f x ∈ S) : fix f ∈ S := sorry

namespace ChainClosed

theorem empty : ChainClosed (∅ : Set D) := sorry

/-- Proposition 19: inverse images along continuous maps preserve
chain-closure. -/
theorem preimage {f : D → E} (hf : ωScottContinuous f) {S : Set E} (hS : ChainClosed S) :
    ChainClosed (f ⁻¹' S) := sorry

/-- In a cpo with no infinite strictly ascending chains, such as a flat domain,
every subset is chain-closed: the lub of a chain is one of its elements. -/
theorem of_wellFoundedGT [WellFoundedGT D] (S : Set D) : ChainClosed S := sorry

/-- In the domain of partial functions (Example 4), the partial functions
whose graph lies inside a fixed relation `R` form a chain-closed set, since the
graph of a lub is the union of the graphs. With Theorem 17 this proves that
every output of a recursively defined partial function satisfies `R`. -/
theorem graph_subset {X Y : Type*} (R : Set (X × Y)) :
    ChainClosed {w : X → Part Y | graph w ⊆ R} := sorry

/-- Chain-closure is the chain-indexed instance of Mathlib's closure under
directed sups, over the same family of sets `ωScottContinuous` is defined over. -/
theorem iff_dirSupClosedOn {S : Set D} :
    ChainClosed S ↔ DirSupClosedOn (Set.range fun c : Chain D => Set.range c) S := sorry

/-- A chain-closed set is an ω-cpo with the lubs of the ambient ω-cpo. Not an
instance, since it depends on the proof of chain-closure. -/
abbrev omegaCompletePartialOrder {S : Set D} (h : ChainClosed S) :
    OmegaCompletePartialOrder S :=
  OmegaCompletePartialOrder.subtype (· ∈ S) fun c hc => h c fun n => hc _ ⟨n, rfl⟩

/-- The inclusion of a chain-closed set is continuous. -/
theorem ωScottContinuous_val {S : Set D} (h : ChainClosed S) :
    @ωScottContinuous S D h.omegaCompletePartialOrder _ Subtype.val := sorry

end ChainClosed

section Examples

/-- The even natural numbers inside `Ω`. -/
def omegaEvens : Set ℕ∞ := {x | ∃ n : ℕ, x = ((2 * n : ℕ) : ℕ∞)}

/-- The chain `0 ⊑ 2 ⊑ 4 ⊑ …` in `Ω`. -/
noncomputable def omegaEvenChain : Chain ℕ∞ :=
  ⟨⟨fun n => ((2 * n : ℕ) : ℕ∞), fun _ _ h => ENat.natCast_le_natCast.2 (by omega)⟩⟩

end Examples

section Exercises

/-- The finite diagonal of `Ω × Ω`. -/
def finiteDiagonal : Set (ℕ∞ × ℕ∞) := {p | ∃ n : ℕ, p = ((n : ℕ∞), (n : ℕ∞))}

end Exercises

end Silmaril.Semantics.Domain
