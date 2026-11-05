module

public import Silmaril.Calculi.PCF.Denotation

/-! # Parallel and sequential or

Parallel or (`porFun`, defined with the model in `Silmaril.Calculi.PCF.Denotation`)
is `true` as soon as either argument is `true`, even if the other is `⊥`.
The left sequential or `orFun` is the denotation of the PCF term
`Term.seqOr = fun x:bool. fun y:bool. if x then true else y`, which inspects
its first argument first and so is `⊥` whenever that argument is. The two
agree on defined arguments and differ at `(⊥, true)`.

Parallel or is also the curried domain element
`porElem ∈ ⟦bool -> bool -> bool⟧`, the denotation of
`fun x:bool. fun y:bool. por(x, y)` in PCF+`por` (`denote_porLam`). It is
characterized by three entries of its table (`PorSpec`): a monotone function
with `p(true)(⊥) = true`, `p(⊥)(true) = true` and `p(false)(false) = false`
is parallel or (`eq_porFun_of_monotone`, `PorSpec.eq_porElem`). By
`Silmaril.Calculi.PCF.por_undefinable` no PCF term denotes it.

The notes' numbered items on these functions (Definitions 32 and 33,
Exercise 20) are restated in
`Silmaril.Courses.Cambridge.DenotationalSemantics.FullAbstraction`.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The type `bool -> bool -> bool` of binary boolean functions. -/
abbrev Ty.bool₂ : Ty := Ty.bool ⤳ Ty.bool ⤳ Ty.bool

/-- Apply an element of `⟦bool -> bool -> bool⟧` to two arguments
(`Model.app₂` at this type). -/
noncomputable abbrev app₂ {p : Bool} (f : (model p).ty Ty.bool₂) (x y : Part Bool) :
    Part Bool :=
  Model.app₂ (M := model p) (σ := Ty.bool) (σ' := Ty.bool) (τ := Ty.bool) f x y

/-- On defined arguments parallel or is boolean disjunction. -/
@[simp] theorem porFun_some_some (a b : Bool) :
    porFun (.some a) (.some b) = .some (a || b) := sorry

/-- Parallel or as a curried domain element, `por ∈ ⟦bool -> bool -> bool⟧`. -/
noncomputable def porElem {p : Bool} : (model p).ty Ty.bool₂ :=
  curry porHom

section SeqOr

variable {p : Bool} {Γ : Ctx Base}

/-- The PCF term `fun x:bool. fun y:bool. if x then true else y`. -/
def Term.seqOr : Term p Γ Ty.bool₂ :=
  .lam (.lam (Term.ite (.var (.succ .zero)) Term.tt Term.v0))

/-- The left sequential or, `or(x, y) = if(x, true, y)`. -/
noncomputable def orFun (x y : Part Bool) : Part Bool := condFun x (.some true) y

/-- `or` is the denotation of `Term.seqOr`. -/
theorem denote_seqOr (ρ : (model p).Env Γ) (x y : Part Bool) :
    app₂ (denote (Term.seqOr : Term p Γ _) ρ) x y = orFun x y := sorry

end SeqOr

section Characterization

/-- The three entries of the table of parallel or that characterize it:
`f(true)(⊥) = true`, `f(⊥)(true) = true` and `f(false)(false) = false`. -/
def PorSpec {p : Bool} (f : (model p).ty Ty.bool₂) : Prop :=
  app₂ f (.some true) .none = .some true ∧ app₂ f .none (.some true) = .some true ∧
    app₂ f (.some false) (.some false) = .some false

/-- `PorSpec f` holds exactly when `f` is parallel or. -/
theorem porSpec_iff {p : Bool} {f : (model p).ty Ty.bool₂} : PorSpec f ↔ f = porElem := sorry

end Characterization

section PorTerm

variable {Γ : Ctx Base}

/-- The PCF+`por` term `fun x:bool. fun y:bool. por(x, y)`. -/
def Term.porLam : Term true Γ Ty.bool₂ :=
  .lam (.lam (Term.por (.var (.succ .zero)) Term.v0))

end PorTerm

end Silmaril.Calculi.PCF
