module

public import Silmaril.Calculi.TypedLambda.Raw
public import Silmaril.Calculi.PCF.BigStep
public import Mathlib.Data.Nat.Factorial.Basic

/-! # Programming in PCF

Exercise 13 of the denotational semantics notes (§5.4): addition,
multiplication and factorial, defined with `fix`, compute what they should.
Exercise 12 (Church's `Y` is not typable) holds for every signature and is
`Silmaril.Calculi.TypedLambda.Raw.not_hasType_churchY`.

Arguments are passed by name, so a PCF function may receive an unevaluated
argument. The correctness statements therefore quantify over arbitrary closed
arguments that evaluate to numerals, and specialise to numerals. The
evaluation proofs go through *head expansions* (`Expands`): `t'` expands to
`t` if `t'` evaluates to everything `t` evaluates to.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-! ### Head expansion -/

variable {p : Bool} {σ τ : Ty}

/-- `t'` head-expands to `t`: it evaluates to every value that `t` evaluates to. -/
def Expands (t t' : Closed p τ) : Prop := ∀ ⦃v⦄, t ⇓ v → t' ⇓ v

namespace Expands

theorem refl (t : Closed p τ) : Expands t t := sorry

theorem trans {t t' t'' : Closed p τ} (h : Expands t t') (h' : Expands t' t'') :
    Expands t t'' := sorry

/-- Head expansion is a congruence for application. -/
theorem app {t t' : Closed p (σ ⤳ τ)} (h : Expands t t') (u : Closed p σ) :
    Expands (.app t u) (.app t' u) := sorry

/-- β-expansion, by rule Fun. -/
theorem beta (t : Term p [σ] τ) (u : Closed p σ) : Expands (t.subst1 u) (.app (.lam t) u) := sorry

end Expands

/-! ### Exercise 13 -/

namespace Term

/-- The variable bound by the second innermost binder. -/
abbrev v1 {Γ : Ctx Base} {σ σ' : Ty} : Term p (σ' :: σ :: Γ) σ := .var (.succ .zero)

/-- The variable bound by the third innermost binder. -/
abbrev v2 {Γ : Ctx Base} {σ σ' σ'' : Ty} : Term p (σ'' :: σ' :: σ :: Γ) σ :=
  .var (.succ (.succ .zero))

end Term

/-- Exercise 13: `plus = fun x:nat. fix(fun (p:nat -> nat) (y:nat).
if zero?(y) then x else succ(p pred(y)))`. -/
def plus : Closed p (Ty.nat ⤳ Ty.nat ⤳ Ty.nat) :=
  .lam (Term.fix (.lam (.lam
    (Term.ite (Term.isZero Term.v0) Term.v2 (Term.succ (.app Term.v1 (Term.pred Term.v0)))))))

/-- Exercise 13: `mul = fun x:nat. fix(fun (t:nat -> nat) (y:nat).
if zero?(y) then 0 else plus x (t pred(y)))`. -/
def mul : Closed p (Ty.nat ⤳ Ty.nat ⤳ Ty.nat) :=
  .lam (Term.fix (.lam (.lam (Term.ite (Term.isZero Term.v0) Term.zero
    (.app (.app (TypedLambda.Term.ofClosed plus) Term.v2) (.app Term.v1 (Term.pred Term.v0)))))))

/-- Exercise 13: a factorial function, `fact = fix(fun (f:nat -> nat) (y:nat).
if zero?(y) then 1 else mul y (f pred(y)))`. -/
def fact : Closed p (Ty.nat ⤳ Ty.nat) :=
  Term.fix (.lam (.lam (Term.ite (Term.isZero Term.v0) (Term.num 1)
    (.app (.app (TypedLambda.Term.ofClosed mul) Term.v0) (.app Term.v1 (Term.pred Term.v0))))))

/-- The recursive part of `plus x` for a closed `x`. -/
def plusRec (a : Closed p .nat) : Closed p ((Ty.nat ⤳ Ty.nat) ⤳ Ty.nat ⤳ Ty.nat) :=
  .lam (.lam (Term.ite (Term.isZero Term.v0) (TypedLambda.Term.ofClosed a)
    (Term.succ (.app Term.v1 (Term.pred Term.v0)))))

/-- The recursive part of `mul x` for a closed `x`. -/
def mulRec (a : Closed p .nat) : Closed p ((Ty.nat ⤳ Ty.nat) ⤳ Ty.nat ⤳ Ty.nat) :=
  .lam (.lam (Term.ite (Term.isZero Term.v0) Term.zero
    (.app (.app (TypedLambda.Term.ofClosed plus) (TypedLambda.Term.ofClosed a))
      (.app Term.v1 (Term.pred Term.v0)))))

end Silmaril.Calculi.PCF
