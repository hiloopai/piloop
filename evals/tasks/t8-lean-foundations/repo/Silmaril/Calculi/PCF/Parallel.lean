module

public import Silmaril.Calculi.PCF.Denotation
public import Silmaril.Calculi.PCF.Por

/-! # Parallel boolean combinators in PCF+`por`

Derived term formers used to build definable elements:

* `Term.not t = if t then false else true`, the strict negation;
* `Term.pand t u = ¬por(¬t, ¬u)`, parallel and, which is `false` as soon as
  either argument is `false`, even if the other is `⊥`;
* `Term.porList` and `Term.pandList`, iterated parallel or and and;
* `Term.ifChain [(G₁, w₁), …, (Gₖ, wₖ)] = if G₁ then w₁ else … else Ω`;
* `Term.eqNum k t`, the test `t = k̄` on numerals.

Each comes with its denotation, stated for `denote` into the flat domains
`Part Bool` and `Part ℕ`.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {p : Bool} {Γ : Ctx Base} {τ : Ty}

/-- Strict negation, `if t then false else true`. -/
def Term.not (t : Term p Γ .bool) : Term p Γ .bool := Term.ite t Term.ff Term.tt

/-- `⟦¬t⟧ = ¬⊥ ⟦t⟧`. -/
theorem denote_not (t : Term p Γ .bool) (ρ : (model p).Env Γ) :
    (denote (Term.not t) ρ : Part Bool) = Flat.map (!·) (denote t ρ : Part Bool) := sorry

/-- Parallel and, `¬por(¬a, ¬b)`. -/
def Term.pand (t u : Term true Γ .bool) : Term true Γ .bool :=
  Term.not (Term.por (Term.not t) (Term.not u))

/-- The value of parallel and: `¬por(¬x, ¬y)`. -/
theorem denote_pand (t u : Term true Γ .bool) (ρ : (model true).Env Γ) :
    (denote (Term.pand t u) ρ : Part Bool) =
      Flat.map (!·) (porFun (Flat.map (!·) (denote t ρ : Part Bool))
        (Flat.map (!·) (denote u ρ : Part Bool))) := sorry

/-- Parallel and is `true` exactly when both arguments are. -/
theorem denote_pand_eq_true_iff (t u : Term true Γ .bool) (ρ : (model true).Env Γ) :
    (denote (Term.pand t u) ρ : Part Bool) = .some true ↔
      denote t ρ = (.some true : Part Bool) ∧ denote u ρ = (.some true : Part Bool) := sorry

/-- Parallel and is `false` as soon as its left argument is. -/
theorem denote_pand_of_left_false {t u : Term true Γ .bool} {ρ : (model true).Env Γ}
    (h : denote t ρ = (.some false : Part Bool)) :
    (denote (Term.pand t u) ρ : Part Bool) = .some false := sorry

/-- Parallel and is `false` as soon as its right argument is. -/
theorem denote_pand_of_right_false {t u : Term true Γ .bool} {ρ : (model true).Env Γ}
    (h : denote u ρ = (.some false : Part Bool)) :
    (denote (Term.pand t u) ρ : Part Bool) = .some false := sorry

/-- Iterated parallel or, `por(t₁, por(t₂, … false))`. -/
def Term.porList : List (Term true Γ .bool) → Term true Γ .bool
  | [] => Term.ff
  | t :: l => Term.por t (Term.porList l)

/-- An iterated parallel or is `true` exactly when some disjunct is. -/
theorem denote_porList_eq_true_iff (l : List (Term true Γ .bool)) (ρ : (model true).Env Γ) :
    (denote (Term.porList l) ρ : Part Bool) = .some true ↔
      ∃ t ∈ l, denote t ρ = (.some true : Part Bool) := sorry

/-- An iterated parallel or is `false` when every disjunct is. -/
theorem denote_porList_of_forall_false {l : List (Term true Γ .bool)} {ρ : (model true).Env Γ}
    (h : ∀ t ∈ l, denote t ρ = (.some false : Part Bool)) :
    (denote (Term.porList l) ρ : Part Bool) = .some false := sorry

/-- Iterated parallel and, `pand(t₁, pand(t₂, … true))`. -/
def Term.pandList : List (Term true Γ .bool) → Term true Γ .bool
  | [] => Term.tt
  | t :: l => Term.pand t (Term.pandList l)

/-- An iterated parallel and is `true` exactly when every conjunct is. -/
theorem denote_pandList_eq_true_iff (l : List (Term true Γ .bool)) (ρ : (model true).Env Γ) :
    (denote (Term.pandList l) ρ : Part Bool) = .some true ↔
      ∀ t ∈ l, denote t ρ = (.some true : Part Bool) := sorry

/-- An iterated parallel and is `false` as soon as some conjunct is. -/
theorem denote_pandList_of_exists_false {l : List (Term true Γ .bool)}
    {ρ : (model true).Env Γ} (h : ∃ t ∈ l, denote t ρ = (.some false : Part Bool)) :
    (denote (Term.pandList l) ρ : Part Bool) = .some false := sorry

/-- `if G₁ then w₁ else if G₂ then w₂ … else Ω`. -/
def Term.ifChain : List (Term p Γ .bool × Term p Γ τ) → Term p Γ τ
  | [] => Term.omega τ
  | q :: l => Term.ite q.1 q.2 (Term.ifChain l)

/-- An if-chain whose guards are all defined, some `true`, and whose branches
under `true` guards agree, denotes that common value. -/
theorem denote_ifChain_eq {l : List (Term p Γ .bool × Term p Γ τ)} {ρ : (model p).Env Γ}
    {val : (model p).ty τ}
    (hall : ∀ q ∈ l, (denote q.1 ρ = (.some true : Part Bool) ∧ denote q.2 ρ = val) ∨
      denote q.1 ρ = (.some false : Part Bool))
    (hex : ∃ q ∈ l, denote q.1 ρ = (.some true : Part Bool)) :
    denote (Term.ifChain l) ρ = val := sorry

/-- An if-chain none of whose guards is `true` denotes `⊥`. -/
theorem denote_ifChain_eq_bot {l : List (Term p Γ .bool × Term p Γ τ)} {ρ : (model p).Env Γ}
    (h : ∀ q ∈ l, denote q.1 ρ ≠ (.some true : Part Bool)) :
    denote (Term.ifChain l) ρ = ⊥ := sorry

/-- `⟦eqNum k t⟧ = (⟦t⟧ == k)`, strict in `⟦t⟧`. -/
theorem denote_eqNum (k : ℕ) (t : Term p Γ .nat) (ρ : (model p).Env Γ) :
    (denote (Term.eqNum k t) ρ : Part Bool) = Flat.map (· == k) (denote t ρ : Part ℕ) := sorry

end Silmaril.Calculi.PCF
