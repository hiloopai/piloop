module

public import Silmaril.Semantics.Domain.Omega
public import Silmaril.Calculi.Imp.Denotation


/-! # §2 Least fixed points (pp. 12–22)

Index of §2 of the notes against `Silmaril.Semantics.Domain`. Each numbered item has a
wrapper named after it. Theorems restate the notes' statement and are proved
by the general development; definitions are aliases of the Mathlib or
Silmaril notion, followed by `example`s that check the defining property.

Posets, monotone maps, least elements and fixed points are Mathlib's. Chains
are Mathlib's `OmegaCompletePartialOrder.Chain`, a monotone sequence `ℕ →o D`,
and the lub of a chain `c` is `IsLUB (Set.range c)`. A cpo is Mathlib's
`OmegaCompletePartialOrder`, which records the lub `ωSup` as data; since lubs
are unique this is no extra structure (`ofChainComplete`). Partial functions
`X ⇀ Y` are `X → Part Y`, and the vertical naturals `Ω` are `ℕ∞`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.DenotationalSemantics.LeastFixedPoints

open Silmaril.Calculi

open OmegaCompletePartialOrder Silmaril.Semantics.Domain

universe u

variable {D E : Type*}

/-! ## §2.1 Posets and monotone functions -/

/-- Definition 1 (Partially ordered set, p. 12): a reflexive, transitive and
antisymmetric relation `⊑` on `D`. -/
abbrev definition1 (D : Type u) : Type u := PartialOrder D

/-- Example 1 (Domain of partial functions, p. 12): `X ⇀ Y` is ordered by
`f ⊑ g` iff `graph(f) ⊆ graph(g)`. -/
theorem example1 {X Y : Type*} {f g : X → Part Y} : f ≤ g ↔ graph f ⊆ graph g := sorry

/-- Definition 2 (Monotone function, p. 13). -/
abbrev definition2 [Preorder D] [Preorder E] (f : D → E) : Prop := Monotone f

/-- Example 2 (p. 13): constant functions are monotone. -/
theorem example2 [Preorder D] [Preorder E] (e : E) : Monotone fun _ : D => e := sorry

/-- Example 3 (p. 13): `F_{b,c}` on `State ⇀ State` is monotone. -/
theorem example3 {L : Type*} (b : Imp.State L → Bool) (c : Imp.State L → Part (Imp.State L)) :
    Monotone (Imp.whileFun b c) := sorry

/-! ## §2.2 Least elements and pre-fixed points -/

/-- Definition 3 (Least element, p. 13): `d ∈ S` and `∀ x ∈ S. d ⊑ x`. -/
abbrev definition3 [PartialOrder D] (S : Set D) (d : D) : Prop := IsLeast S d

/-- Definition 4 (Fixed point, p. 13): `f(d) = d`. -/
abbrev definition4 (f : D → D) (d : D) : Prop := Function.IsFixedPt f d

/-- Definition 5 ((Least) pre-fixed point, p. 13): `fix(f)` is the least `d`
with `f(d) ⊑ d`. -/
abbrev definition5 [PartialOrder D] (f : D → D) (d : D) : Prop := IsLeastPrefixedPoint f d

/-- Proposition 1 (Least pre-fixed points are least fixed points, p. 14): if
`f` is monotone, then `fix(f)` is a fixed point of `f`, and the least one. -/
theorem proposition1 [PartialOrder D] {f : D → D} {d : D} (hf : Monotone f)
    (h : IsLeastPrefixedPoint f d) : f d = d ∧ IsLeast (Function.fixedPoints f) d := sorry

/-! ## §2.3 Least upper bounds -/

/-- Definition 6 (Least upper bound of a chain, p. 14): a chain is a monotone
sequence `d₀ ⊑ d₁ ⊑ d₂ ⊑ …`; its lub is `IsLUB (Set.range c)`. -/
abbrev definition6 (D : Type*) [Preorder D] : Type _ := Chain D

/-- Proposition 3 (Discarding elements, p. 15): `⨆_{n ≥ 0} dₙ = ⨆_{n ≥ 0} d_{N+n}`,
provided either exists. -/
theorem proposition3 [PartialOrder D] (d : Chain D) (N : ℕ) {a : D} :
    IsLUB (Set.range fun n => d (N + n)) a ↔ IsLUB (Set.range d) a := sorry

/-- Proposition 4 (Eventually constant chain, p. 15): if `dₙ = d_N` for all
`n ≥ N`, then `⨆ₙ dₙ = d_N`. -/
theorem proposition4 [PartialOrder D] {d : Chain D} {N : ℕ} (h : ∀ n, N ≤ n → d n = d N) :
    IsLUB (Set.range d) (d N) := sorry

/-- Proposition 5 (Diagonalisation, p. 15): for a doubly indexed family with
`m ≤ m' ∧ n ≤ n' ⇒ d_{m,n} ⊑ d_{m',n'}` (†), the lubs of the rows and of the
columns form chains, and `⨆ₘ ⨆ₙ d_{m,n} = ⨆ₖ d_{k,k} = ⨆ₙ ⨆ₘ d_{m,n}`, assuming
the lubs exist. -/
theorem proposition5 [PartialOrder D] (d : ℕ → ℕ → D)
    (hd : ∀ m m' n n', m ≤ m' → n ≤ n' → d m n ≤ d m' n') {r s : ℕ → D} {δ : D}
    (hr : ∀ m, IsLUB (Set.range (d m)) (r m)) (hs : ∀ n, IsLUB (Set.range (d · n)) (s n))
    (hδ : IsLUB (Set.range fun k => d k k) δ) :
    Monotone r ∧ Monotone s ∧ IsLUB (Set.range r) δ ∧ IsLUB (Set.range s) δ := sorry

/-! ## §2.4 Complete partial orders and domains -/

/-- Definition 7 (Cpos, p. 16): a poset in which every chain has a lub. -/
abbrev definition7 (D : Type u) : Type u := OmegaCompletePartialOrder D

/-- Definition 8 (Domain, p. 17): a cpo with a least element. -/
abbrev definition8 : Type (u + 1) := DomainCat.{u}

/-- Example 4 (Domain of partial functions, p. 17): `⊥` is the totally
undefined function, and the lub of `f₀ ⊑ f₁ ⊑ …` is defined at `x` with value
`y` iff some `fₙ` is. -/
theorem example4 {X Y : Type*} (c : Chain (X → Part Y)) (x : X) (y : Y) :
    (⊥ : X → Part Y) x = Part.none ∧ (y ∈ ωSup c x ↔ ∃ n, y ∈ c n x) := sorry

/-- Example 5 (Finite cpos, p. 17): every chain in a finite poset has a lub. -/
theorem example5 [PartialOrder D] [Finite D] (c : Chain D) : ∃ a, IsLUB (Set.range c) a := sorry

/-- Example 5 (p. 17): the finite cpo with two incomparable minimal elements
below a top element has no least element. -/
theorem example5_wedge : ¬ ∃ b : Wedge, IsLeast Set.univ b := sorry

/-- Example 6 (Flat natural numbers, p. 18): in `ℕ⊥`, `x ⊑ y` iff `x = ⊥` or
`x = y`. -/
theorem example6 {x y : Part ℕ} : x ≤ y ↔ x = ⊥ ∨ x = y := sorry

/-- Example 6 (p. 18): a partial function `X ⇀ ℕ` is the same as a monotone
function from the discrete poset `(X, =)` to `ℕ⊥`. -/
def example6_partialFun (X : Type*) : (X → Part ℕ) ≃ (Discrete X →o Part ℕ) :=
  Flat.partialFunEquiv

/-- Example 7 (Non-example: natural numbers, p. 18): the chain `0 ≤ 1 ≤ 2 ≤ …`
has no upper bound in `ℕ`, so `ℕ` is not a cpo. -/
theorem example7 : ¬ BddAbove (Set.range natChain) := sorry

/-- Example 9 (Non-example: no least upper bound, p. 18): with two upper bounds
`ω₁` and `ω₂` adjoined to `ℕ`, the chain `0 ⊑ 1 ⊑ 2 ⊑ …` has both as upper
bounds but no lub. -/
theorem example9 :
    (∀ b, TwoTops.top b ∈ upperBounds (Set.range twoTopsChain)) ∧
      ¬ ∃ a, IsLUB (Set.range twoTopsChain) a := sorry

/-! ## §2.5 Continuous functions -/

/-- Definition 9 (Continuity, p. 19). -/
abbrev definition9 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E] (f : D → E) :
    Prop :=
  ωScottContinuous f

/-- Definition 10 (Strictness, p. 19): `f(⊥) = ⊥`. -/
abbrev definition10 [Bot D] [Bot E] (f : D → E) : Prop := Strict f

/-- Remark 2 (p. 19): for monotone `f`, `⨆ₙ f(dₙ) ⊑ f(⨆ₙ dₙ)`, so continuity
only needs the converse inequality. -/
theorem remark2 [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder E] {f : D → E}
    (hf : Monotone f) :
    (∀ c : Chain D, ωSup (c.map ⟨f, hf⟩) ≤ f (ωSup c)) ∧
      ((∀ c : Chain D, f (ωSup c) ≤ ωSup (c.map ⟨f, hf⟩)) → ωScottContinuous f) := sorry

/-- Example 11 (p. 20): `F_{b,c}` on `State ⇀ State` is continuous. -/
theorem example11 {L : Type*} (b : Imp.State L → Bool) (c : Imp.State L → Part (Imp.State L)) :
    ωScottContinuous (Imp.whileFun b c) := sorry

/-- Example 12 (Non-example, p. 20): `f : Ω → Ω` with `f(n) = 0` and
`f(ω) = ω` is monotone and strict, but not continuous. -/
theorem example12 :
    (∀ n : ℕ, collapse n = 0) ∧ collapse ⊤ = ⊤ ∧ Monotone collapse ∧ Strict collapse ∧
      ¬ ωScottContinuous collapse := sorry

/-! ## §2.6 Kleene's fixed point theorem -/

/-- Theorem 6 (Kleene's fixed point theorem, p. 21): a continuous `f : D → D`
on a domain has a least pre-fixed point, `fix(f) = ⨆ₙ fⁿ(⊥)`. By Proposition 1
it is also the least fixed point. -/
theorem theorem6 [OmegaCompletePartialOrder D] [OrderBot D] (f : D →𝒄 D) :
    (∀ n, kleeneChain f n = f^[n] ⊥) ∧ fix f = ωSup (kleeneChain f) ∧
      IsLeastPrefixedPoint f (fix f) ∧ IsLeast (Function.fixedPoints f) (fix f) := sorry

/-- Example 13 (p. 22): `F_{⟦B⟧,⟦C⟧}` is continuous, and
`⟦while B do C⟧ = fix(F_{⟦B⟧,⟦C⟧})` is its least fixed point. -/
theorem example13 {L : Type*} [DecidableEq L] (b : Imp.Bexp L) (c : Imp.Prog L) :
    Imp.Prog.denote (.while b c) = fix (Imp.F b.denote c.denote) ∧
      IsLeast (Function.fixedPoints (Imp.F b.denote c.denote)) (Imp.Prog.denote (.while b c)) := sorry

/-! ## §2.7 Exercises -/

/-- Exercise 1 (p. 22): the claims of Examples 1 and 4: `X ⇀ Y` is a poset
under graph inclusion, the pointwise union is the lub of a chain, and the
totally undefined function is least. -/
theorem exercise1 {X Y : Type*} :
    (∀ f g : X → Part Y, f ≤ g ↔ graph f ⊆ graph g) ∧
      (∀ (c : Chain (X → Part Y)) x y, y ∈ ωSup c x ↔ ∃ n, y ∈ c n x) ∧
      ∀ f : X → Part Y, ⊥ ≤ f := sorry

/-- Exercise 2 (p. 22): Remark 1 (iv) and Propositions 2–4 (see `remark1`,
`proposition2`, `proposition3` and `proposition4`), phrased in a cpo. -/
theorem exercise2 [OmegaCompletePartialOrder D] (c c' : Chain D) (N : ℕ) :
    (∀ a, IsLUB (Set.range c) a → a = ωSup c) ∧
      ((∀ n, c n ≤ c' n) → ωSup c ≤ ωSup c') ∧ ωSup (Chain.drop c N) = ωSup c ∧
      ((∀ n, N ≤ n → c n = c N) → ωSup c = c N) := sorry

/-- Exercise 3 (p. 22): `F_{b,c}` is monotone and continuous. It is strict
exactly when `b` holds everywhere (here `b` is total, since Imp's expressions
always terminate). -/
theorem exercise3 {L : Type*} (b : Imp.State L → Bool) (c : Imp.State L → Part (Imp.State L)) :
    Monotone (Imp.whileFun b c) ∧ ωScottContinuous (Imp.whileFun b c) ∧
      (Strict (Imp.F b c) ↔ ∀ s, b s = true) := sorry

end Silmaril.Courses.Cambridge.DenotationalSemantics.LeastFixedPoints
