module

public import Silmaril.Semantics.Domain.Omega


/-! # §3 Constructions on domains (pp. 23–30)

Index of §3 of the notes against `Silmaril.Semantics.Domain`. The flat domain `X⊥` is
Mathlib's `Part X`; binary, general and function domains are Mathlib's `×`,
`Π` and `→𝒄` ω-cpos. Continuity of a map into `→𝒄` is part of its type, so
"well-defined and continuous" statements become the existence of a bundled
map together with its defining equation.

Corrections to the notes:

* Proposition 11 (the conditional) is cited on pp. 30 and 50 but is not
  printed; `proposition11` states it.
* Proposition 15 prints the composite as `λd. g(f(d))`, which does not
  typecheck; the intended composite is `λd. f(g(d))`.
* Exercise 5, alternative (b), asks for constancy on `X` only, which does not
  imply monotonicity (take `f(⊥) = 0` and `f(x) = 1`). The formalized
  statement asks for `f(d) = f(⊥)` for all `d ∈ X⊥`.

Remark beyond the notes: the bijection of Exercise 6 is order-reversing, since
a more defined predicate has a smaller threshold (`exercise6_antitone`).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.Constructions


open OmegaCompletePartialOrder Silmaril.Semantics.Domain

universe u

variable {D D' E F : Type*}

/-! ## §3.1 Flat domains -/

/-- Definition 11 (Discrete cpo, p. 23): equality as a partial order. -/
abbrev definition11 (X : Type u) : Type u := Discrete X

/-- Definition 12 (Flat domain, p. 23): `X⊥` is `Part X`, with `x ⊑ x'` iff
`x = ⊥` or `x = x'`. -/
abbrev definition12 (X : Type u) : Type u := Part X

/-- Proposition 7 (Flat domain lifting, p. 24): a partial function `f : X ⇀ Y`
lifts to a strict continuous `f⊥ : X⊥ → Y⊥` with `f⊥(x) = f(x)` for `x ∈ X`. -/
theorem proposition7 {X Y : Type*} (f : X → Part Y) :
    ωScottContinuous (Flat.lift f) ∧ Strict (Flat.lift f) ∧
      ∀ x, Flat.lift f (Part.some x) = f x := sorry

/-! ## §3.2 Products of domains -/

/-- Definition 13 (Binary product of two orders, p. 24): `D₁ × D₂`, ordered
componentwise. -/
abbrev definition13 (D : Type*) (E : Type*) : Type _ := D × E

/-- Proposition 8 (Products preserve lubs and least element, p. 24): lubs of
chains in `D₁ × D₂` are computed componentwise, and if `D₁` and `D₂` have least
elements, so does `D₁ × D₂`, with `⊥ = (⊥, ⊥)`. -/
theorem proposition8 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E] :
    (∀ c : Chain (D × E), ωSup c = (ωSup (c.map OrderHom.fst), ωSup (c.map OrderHom.snd))) ∧
      ∀ [OrderBot D] [OrderBot E], (⊥ : D × E) = (⊥, ⊥) := sorry

/-- Proposition 9 (Functions of two arguments, p. 24): `f : D × E → F` is
monotone, respectively continuous, iff it is so in each argument separately. -/
theorem proposition9 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]
    [OmegaCompletePartialOrder F] (f : D × E → F) :
    (Monotone f ↔ (∀ e, Monotone fun d => f (d, e)) ∧ ∀ d, Monotone fun e => f (d, e)) ∧
      (ωScottContinuous f ↔
        (∀ e, ωScottContinuous fun d => f (d, e)) ∧ ∀ d, ωScottContinuous fun e => f (d, e)) := sorry

/-- Proposition 10 (Projections and pairing, p. 25): the projections are
continuous, and so is the pairing `⟨f₁, f₂⟩` of continuous maps. -/
theorem proposition10 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]
    [OmegaCompletePartialOrder F] :
    ωScottContinuous (Prod.fst : E × F → E) ∧ ωScottContinuous (Prod.snd : E × F → F) ∧
      ∀ (f₁ : D →𝒄 E) (f₂ : D →𝒄 F), ωScottContinuous fun d => (f₁ d, f₂ d) := sorry

/-- Definition 14 (General product of posets, p. 26): `∏ᵢ Dᵢ`, ordered
componentwise. -/
abbrev definition14 {I : Type*} (P : I → Type*) : Type _ := ∀ i, P i

/-- Remark 3 (p. 26): a binary product is a `𝔹`-indexed product,
`p ↦ (p true, p false)`. -/
def remark3 {P : Bool → Type*} [∀ b, OmegaCompletePartialOrder (P b)] :
    (∀ b, P b) ≃o P true × P false :=
  piBoolOrderIso

/-- Proposition 12 (General products of cpos and domains, p. 26): lubs in a
product of cpos are computed componentwise, so each projection `πᵢ` is
continuous; if all the `Dᵢ` are domains, the least element of the product maps
each `i` to `⊥`. -/
theorem proposition12 {I : Type*} {P : I → Type*} [∀ i, OmegaCompletePartialOrder (P i)] :
    (∀ (c : Chain (∀ i, P i)) (i : I), ωSup c i = ωSup (c.map (Pi.evalOrderHom i))) ∧
      (∀ i, ωScottContinuous (fun p : ∀ i, P i => p i)) ∧
      ∀ [∀ i, OrderBot (P i)] (i : I), (⊥ : ∀ i, P i) i = ⊥ := sorry

/-! ## §3.3 Function domains -/

/-- Definition 15 (Cpo/domain of continuous functions, p. 27): the continuous
functions `D → E`, ordered pointwise. -/
abbrev definition15 (D : Type*) (E : Type*) [OmegaCompletePartialOrder D]
    [OmegaCompletePartialOrder E] : Type _ :=
  D →𝒄 E

/-- Proposition 11 (cited on pp. 30 and 50, not printed): the conditional
`if : 𝔹⊥ × (D × D) → D`, with `if(true, x, y) = x`, `if(false, x, y) = y` and
`if(⊥, x, y) = ⊥`, is continuous. -/
theorem proposition11 [OmegaCompletePartialOrder D] [OrderBot D] (x y : D) :
    condFun (Part.some true) x y = x ∧ condFun (Part.some false) x y = y ∧
      condFun Part.none x y = ⊥ ∧
      ωScottContinuous fun p : Part Bool × (D × D) => condFun p.1 p.2.1 p.2.2 := sorry

/-- Proposition 13 (Evaluation, p. 28): `eval(f, d) = f(d)` is continuous. -/
theorem proposition13 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E] :
    ωScottContinuous fun p : (D →𝒄 E) × D => p.1 p.2 := sorry

/-- Proposition 14 (Currying, p. 28): for continuous `f : D' × D → E`,
`cur(f)(d') = λd. f(d', d)` is a well-defined continuous map `D' → (D → E)`. -/
theorem proposition14 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder D']
    [OmegaCompletePartialOrder E] (f : D' × D →𝒄 E) :
    (∀ d' d, curry f d' d = f (d', d)) ∧ ωScottContinuous (curry f) := sorry

/-- Proposition 15 (Continuity of composition, p. 28), corrected: composition
`(f, g) ↦ λd. f(g(d))` is continuous. The notes print `λd. g(f(d))`, which
does not typecheck. -/
theorem proposition15 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E]
    [OmegaCompletePartialOrder F] :
    (∀ (f : E →𝒄 F) (g : D →𝒄 E) d, compHom (f, g) d = f (g d)) ∧
      ωScottContinuous fun p : (E →𝒄 F) × (D →𝒄 E) => p.1.comp p.2 := sorry

/-! ## §3.4 Exercises -/

/-- Exercise 4 (p. 30): flat domains, binary products and general products of
domains are domains (they carry `OmegaCompletePartialOrder` and `OrderBot`
instances, found by instance search), and flat lifting (Proposition 7) and the
conditional (Proposition 11) are continuous. -/
theorem exercise4 {X Y : Type*} [OmegaCompletePartialOrder D] [OrderBot D]
    [OmegaCompletePartialOrder E] [OrderBot E] {I : Type*} {P : I → Type*}
    [∀ i, OmegaCompletePartialOrder (P i)] [∀ i, OrderBot (P i)] (f : X → Part Y) :
    Nonempty (OmegaCompletePartialOrder (definition12 X) × OrderBot (definition12 X)) ∧
      Nonempty (OmegaCompletePartialOrder (definition13 D E) × OrderBot (definition13 D E)) ∧
      Nonempty (OmegaCompletePartialOrder (definition14 P) × OrderBot (definition14 P)) ∧
      ωScottContinuous (Flat.lift f) ∧
      ωScottContinuous fun p : Part Bool × (D × D) => condFun p.1 p.2.1 p.2.2 := sorry

/-- Exercise 5 (p. 30), corrected: `f : X⊥ → Y⊥` is continuous iff it is strict
or constant, where constant means `f(d) = f(⊥)` for all `d ∈ X⊥`. -/
theorem exercise5 {X Y : Type*} (f : Part X → Part Y) :
    ωScottContinuous f ↔ Strict f ∨ ∀ d, f d = f ⊥ := sorry

/-- Exercise 6 (p. 30): `Ω → {⊤}⊥` is in bijection with `Ω`. The bijection sends
a predicate to the least point where it is defined. -/
noncomputable def exercise6 : (ℕ∞ →𝒄 Part Unit) ≃ ℕ∞ := OmegaThreshold.equiv

/-- Remark on Exercise 6 (not in the notes): the bijection reverses the order,
since a more defined predicate is defined from an earlier point on. -/
theorem exercise6_antitone : Antitone exercise6 := sorry

/-- Exercise 7 (p. 30): Propositions 14 and 15, currying and composition are
continuous. -/
theorem exercise7 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder D']
    [OmegaCompletePartialOrder E] [OmegaCompletePartialOrder F] :
    ωScottContinuous (curry : (D' × D →𝒄 E) → D' →𝒄 D →𝒄 E) ∧
      ωScottContinuous fun p : (E →𝒄 F) × (D →𝒄 E) => p.1.comp p.2 := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.Constructions
