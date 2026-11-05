module

public import Silmaril.Calculi.TypedLambda.Denotation
public import Silmaril.Calculi.PCF.Morphism
public import Mathlib.Data.Nat.PSub

/-! # The domain model of PCF

§6.2 of the notes. The ground types denote flat domains, `⟦nat⟧ = ℕ⊥` and
`⟦bool⟧ = 𝔹⊥` (Definition 22), realized as `Part ℕ` and `Part Bool`. The
operators denote (Definitions 24 and 26):

* `𝟶`, `true` and `false` denote constants;
* `succ`, `pred` and `zero?` denote the flat liftings of `succ`, `pred` and
  `zero?` (Proposition 7);
* `if` denotes the semantic conditional (Proposition 11);
* `fix` denotes Kleene's least fixed point operator (Theorem 6,
  Proposition 16);
* `por` denotes parallel or (Definition 32).

The model of PCF+`por` restricts to the model of PCF along the inclusion
(`denote_embed`).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- Definition 22 at ground types: the flat domains `ℕ⊥` and `𝔹⊥`. -/
@[reducible] noncomputable def baseDom : Base → DomainCat.{0}
  | .nat => DomainCat.of (Part ℕ)
  | .bool => DomainCat.of (Part Bool)

/-- Definition 24: `pred`, undefined at `0`. -/
def predPart : ℕ → Part ℕ
  | 0 => Part.none
  | n + 1 => Part.some n

/-- `predPart` is Mathlib's partial predecessor `Nat.ppred`, viewed in `Part`. -/
theorem predPart_eq_ppred (n : ℕ) : predPart n = ↑(Nat.ppred n) := sorry

/-- Definition 32: parallel or. -/
noncomputable def porFun (x y : Part Bool) : Part Bool :=
  open Classical in
  if x = Part.some true ∨ y = Part.some true then Part.some true
  else if x = Part.some false ∧ y = Part.some false then Part.some false
  else Part.none

section Por

/-- The table of Definition 32. -/
@[simp] theorem porFun_true_left (y : Part Bool) : porFun (Part.some true) y = Part.some true := sorry

@[simp] theorem porFun_true_right (x : Part Bool) : porFun x (Part.some true) = Part.some true := sorry

@[simp] theorem porFun_false_false :
    porFun (Part.some false) (Part.some false) = Part.some false := sorry

@[simp] theorem porFun_false_none : porFun (Part.some false) Part.none = Part.none := sorry

@[simp] theorem porFun_none_false : porFun Part.none (Part.some false) = Part.none := sorry

@[simp] theorem porFun_none_none : porFun Part.none Part.none = Part.none := sorry

/-- The three flat Booleans; see `Flat.bool_cases`. -/
theorem part_bool_cases (x : Part Bool) :
    x = Part.none ∨ x = Part.some true ∨ x = Part.some false := sorry

/-- `Part.none` is least; see `Flat.none_le`. -/
theorem part_none_le {α : Type*} (x : Part α) : Part.none ≤ x := sorry

theorem porFun_monotone : Monotone fun p : Part Bool × Part Bool => porFun p.1 p.2 := sorry

/-- Definition 32: parallel or is continuous. -/
noncomputable def porHom : Part Bool × Part Bool →𝒄 Part Bool :=
  ContinuousHom.ofFun _ (ωScottContinuous_of_monotone porFun_monotone)

end Por

/-- Definitions 24 and 26: the denotations of the PCF operators. -/
noncomputable def Op.denote {p : Bool} : {as : List Ty} → {τ : Ty} → Op p as τ →
    (TyEnv baseDom as →𝒄 Ty.denote baseDom τ)
  | _, _, .zero => ContinuousHom.const (Part.some 0 : Part ℕ)
  | _, _, .succ => (Flat.map Nat.succ).comp (projHom baseDom .zero)
  | _, _, .pred => (Flat.lift predPart).comp (projHom baseDom .zero)
  | _, _, .tt => ContinuousHom.const (Part.some true : Part Bool)
  | _, _, .ff => ContinuousHom.const (Part.some false : Part Bool)
  | _, _, .isZero => (Flat.map fun n : ℕ => n == 0).comp (projHom baseDom .zero)
  | _, _, .ite _ => cond.comp (pairHom (projHom baseDom .zero)
      (pairHom (projHom baseDom (.succ .zero)) (projHom baseDom (.succ (.succ .zero)))))
  | _, _, .fix _ => fixHom.comp (projHom baseDom .zero)
  | _, _, .por => porHom.comp (pairHom (projHom baseDom .zero) (projHom baseDom (.succ .zero)))

/-- The domain model of PCF, or of PCF+`por`. -/
@[reducible] noncomputable def model (p : Bool) : Model (sig p) := ⟨baseDom, Op.denote⟩

variable {p : Bool} {Γ : Ctx Base} {σ τ : Ty}

/-- The denotation `⟦Γ ⊢ t : τ⟧`. -/
noncomputable abbrev denote (t : Term p Γ τ) : (model p).Env Γ →𝒄 (model p).ty τ :=
  (model p).denote t

/-- The denotation `⟦t⟧ ∈ ⟦τ⟧` of a closed term (Remark 12). -/
noncomputable abbrev denoteClosed (t : Closed p τ) : (model p).ty τ :=
  (model p).denoteClosed t

section Equations

variable (ρ : (model p).Env Γ)

@[simp] theorem denote_tt :
    denote (Term.tt : Term p Γ .bool) ρ = (Part.some true : Part Bool) := sorry

@[simp] theorem denote_ff : denote (Term.ff : Term p Γ .bool) ρ = (Part.some false : Part Bool) := sorry

@[simp] theorem denote_succ (t : Term p Γ .nat) :
    denote (Term.succ t) ρ = Flat.map Nat.succ (denote t ρ : Part ℕ) := sorry

/-- `⟦pred t⟧ = pred⊥(⟦t⟧)`, undefined at `0`. -/
theorem denote_pred (t : Term p Γ .nat) :
    denote (Term.pred t) ρ = Flat.lift predPart (denote t ρ : Part ℕ) := sorry

/-- `⟦zero? t⟧ = (⟦t⟧ == 0)`, strict in `⟦t⟧`. -/
theorem denote_isZero (t : Term p Γ .nat) :
    denote (Term.isZero t) ρ = Flat.map (fun n : ℕ => n == 0) (denote t ρ : Part ℕ) := sorry

/-- `⟦por(t, u)⟧ = por(⟦t⟧, ⟦u⟧)`. -/
theorem denote_por (t u : Term true Γ .bool) (ρ : (model true).Env Γ) :
    denote (Term.por t u) ρ = porFun (denote t ρ : Part Bool) (denote u ρ : Part Bool) := sorry

/-- `⟦if b then t else t'⟧ = if(⟦b⟧, ⟦t⟧, ⟦t'⟧)` (Definition 24). -/
theorem denote_ite (b : Term p Γ .bool) (t t' : Term p Γ τ) :
    denote (Term.ite b t t') ρ = condFun (denote b ρ : Part Bool) (denote t ρ) (denote t' ρ) := sorry

@[simp] theorem denote_num (n : ℕ) :
    denote (Term.num n : Term p Γ .nat) ρ = (Part.some n : Part ℕ) := sorry

@[simp] theorem denote_bool (b : Bool) :
    denote (Term.bool b : Term p Γ .bool) ρ = (Part.some b : Part Bool) := sorry

end Equations

/-- Exercise 15: `⟦Ωτ⟧` is the least element of `⟦τ⟧`. -/
theorem denote_omega (ρ : (model p).Env Γ) :
    denote (Term.omega τ : Term p Γ τ) ρ = (⊥ : (model p).ty τ) := sorry

end Silmaril.Calculi.PCF
