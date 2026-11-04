module

public import Silmaril.Calculi.Imp.Factorial


/-! # §4 Scott induction (pp. 31–36)

Index of §4 of the notes against `Silmaril.Semantics.Domain.ScottInduction` and, for
Example 16, `Silmaril.Calculi.Imp.Factorial`. A subset is `ChainClosed` if it contains
the lub of each of its chains, and `Admissible` if it also contains `⊥`.

Correction to the notes: in the stability step of Example 16, the notes give
`F(w)[X ↦ x, Y ↦ y](Y) = x ⋅ y` when `x > 0`. The value is
`w[X ↦ x - 1, Y ↦ x ⋅ y](Y)`, which is non-negative because `w ∈ S` and
`x ⋅ y ≥ 0`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.ScottInduction

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain Silmaril.Calculi.Imp.Factorial

variable {D E : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]

/-! ## §4.1 Reasoning on fixed points -/

/-- Theorem 17 (Scott induction, p. 31): if `S ⊆ D` contains `⊥`, is
chain-closed and is stable for the continuous `f`, then `fix(f) ∈ S`. -/
theorem theorem17 [OrderBot D] {f : D →𝒄 D} {S : Set D} (hbot : ⊥ ∈ S)
    (hchain : ∀ c : Chain D, (∀ n, c n ∈ S) → ωSup c ∈ S) (hstable : ∀ x ∈ S, f x ∈ S) :
    fix f ∈ S := sorry

/-- Remark 4 (p. 31): a set containing `⊥` and chain-closed is *admissible*;
Scott induction also applies to properties. -/
theorem remark4 [OrderBot D] {f : D →𝒄 D} {S : Set D} {Φ : D → Prop} :
    (Admissible S ↔ ⊥ ∈ S ∧ ChainClosed S) ∧
      (Φ ⊥ → ChainClosed {x | Φ x} → (∀ x, Φ x → Φ (f x)) → Φ (fix f)) := sorry

/-- Example 14 (p. 31): in `Ω`, every finite subset is chain-closed,
`{0, 2, 4, …}` is not chain-closed, and `{0, 2, 4, …} ∪ {ω}` is admissible. -/
theorem example14 :
    (∀ S : Set ℕ∞, S.Finite → ChainClosed S) ∧ ¬ ChainClosed omegaEvens ∧
      Admissible (insert ⊤ omegaEvens) := sorry

/-! ## §4.2 Building chain-closed subsets -/

/-- Proposition 18 (Basic relations, p. 32): `{(x, y) | x ⊑ y}` and
`{(x, y) | x = y}` are chain-closed in `D × D`. -/
theorem proposition18 :
    ChainClosed {p : D × D | p.1 ≤ p.2} ∧ ChainClosed {p : D × D | p.1 = p.2} := sorry

/-- Proposition 19 (Inverse image and substitution, p. 32): the inverse image
of a chain-closed set under a continuous function is chain-closed. -/
theorem proposition19 {f : D → E} (hf : ωScottContinuous f) {S : Set E} (hS : ChainClosed S) :
    ChainClosed (f ⁻¹' S) := sorry

/-- Proposition 20 (Logical operations, p. 32): chain-closed sets are closed
under binary unions and intersections. -/
theorem proposition20 {S T : Set D} (hS : ChainClosed S) (hT : ChainClosed T) :
    ChainClosed (S ∪ T) ∧ ChainClosed (S ∩ T) := sorry

/-- §4.2 (p. 32): chain-closed sets are closed under arbitrary intersections. -/
theorem chainClosed_iInter {ι : Sort*} {S : ι → Set D} (hS : ∀ i, ChainClosed (S i)) :
    ChainClosed (⋂ i, S i) := sorry

/-- Proposition 21 (Universal quantification, p. 32): if `P(x, y)` determines a
chain-closed subset of `D × E`, then `∀ x ∈ D. P(x, y)` determines a
chain-closed subset of `E`. -/
theorem proposition21 {P : D × E → Prop} (hP : ChainClosed {p | P p}) :
    ChainClosed {e | ∀ d, P (d, e)} := sorry

/-- §4.2 (p. 33): for continuous `fᵢ, g : D → E` and `e ∈ E`, the formula
`Φ(x) ≔ ∀ y ∈ E, (∀ i ∈ I, fᵢ(x) ⊑ y) ∨ g(x) = e` is chain-closed. -/
theorem phi_chainClosed {ι : Type*} {f : ι → D → E} {g : D → E} {e : E}
    (hf : ∀ i, ωScottContinuous (f i)) (hg : ωScottContinuous g) :
    ChainClosed {x | ∀ y, (∀ i, f i x ≤ y) ∨ g x = e} := sorry

/-! ## §4.3 Using Scott induction -/

/-- Example 15 (Revisiting the least fixed point property, p. 33): Scott
induction on the down-set `d↓` proves (lfp-least). -/
theorem example15 [OrderBot D] {f : D →𝒄 D} {d : D} (h : f d ≤ d) : fix f ≤ d := sorry

/-- Example 16 (p. 34): for the functional `F` of the factorial loop,
`∀x. ∀y ≥ 0. fix(F)[X ↦ x, Y ↦ y] ⇓ ⟹ (fix(F)[X ↦ x, Y ↦ y])(Y) ≥ 0`. -/
theorem example16 (x y : ℤ) (hy : 0 ≤ y) (h : (fix factF (st x y)).Dom) :
    0 ≤ (fix factF (st x y)).get h .Y := sorry

/-- Example 17 (p. 35): if `f ∘ g ⊑ g ∘ f` then `f(⊥) ⊑ g(⊥) ⟹ fix(f) ⊑ fix(g)`. -/
theorem example17 [OrderBot D] {f g : D →𝒄 D} (hcomm : ∀ x, f (g x) ≤ g (f x))
    (hbot : f ⊥ ≤ g ⊥) : fix f ≤ fix g := sorry

/-! ## §4.4 Exercises -/

/-- Exercise 8 (p. 35): the properties of §4.2: equality and `⊑` are
chain-closed, so are inverse images by continuous functions, binary unions and
arbitrary intersections. -/
theorem exercise8 :
    ChainClosed {p : D × D | p.1 ≤ p.2} ∧ ChainClosed {p : D × D | p.1 = p.2} ∧
      (∀ (f : D → E) (S : Set E), ωScottContinuous f → ChainClosed S →
        ChainClosed (f ⁻¹' S)) ∧
      (∀ S T : Set D, ChainClosed S → ChainClosed T → ChainClosed (S ∪ T)) ∧
      ∀ (ι : Type) (S : ι → Set D), (∀ i, ChainClosed (S i)) → ChainClosed (⋂ i, S i) := sorry

/-- Exercise 9 (p. 35): the image of a chain-closed set under a continuous map
need not be chain-closed. -/
theorem exercise9 :
    ∃ f : Discrete ℕ → ℕ∞, ωScottContinuous f ∧ ChainClosed (Set.univ : Set (Discrete ℕ)) ∧
      ¬ ChainClosed (f '' Set.univ) := sorry

/-- Exercise 10 (p. 36): a subset of `Ω × Ω` whose slices are all chain-closed
but which is not chain-closed itself: the finite diagonal. -/
theorem exercise10 :
    (∀ d, ChainClosed {d' | (d, d') ∈ finiteDiagonal}) ∧
      (∀ d', ChainClosed {d | (d, d') ∈ finiteDiagonal}) ∧ ¬ ChainClosed finiteDiagonal := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.ScottInduction
