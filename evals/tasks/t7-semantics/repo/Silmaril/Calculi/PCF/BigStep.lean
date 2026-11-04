module

public import Silmaril.Calculi.PCF.Syntax

/-! # Evaluation

The big-step evaluation relation `t ⇓ v` of Figure 3 of the notes (§5.2), with
the rules PorL, PorR and PorF of Figure 5 for PCF+`por`. The language flag is
an index, so the `por` rules only exist for `p = true`.

Evaluation relates closed terms to values (`Eval.value`) and is deterministic
(Proposition 23), including for PCF+`por` (Exercise 21), even though PorL and
PorR overlap. `Ω` diverges (Example 18), and evaluation does not reduce under
binders (Example 20). The rules can be inverted for a known operator
(`Eval.app_lam_iff`, `Eval.pred_iff`, `Eval.ite_iff`, ...).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- Figure 3 and Figure 5: the closed term `t` evaluates to the value `v`.

As for `Value`, rules about operators quantify over the argument tuple; the
`Eval.succ'`-style lemmas below restate them with the smart constructors. -/
inductive Eval : {p : Bool} → {τ : Ty} → Closed p τ → Closed p τ → Prop
  /-- Val. -/
  | val {p : Bool} {τ : Ty} {v : Closed p τ} : Value v → Eval v v
  /-- Succ. -/
  | succ {p : Bool} {args : Tuple (Closed p) [.nat]} {v : Closed p .nat} :
      Eval args.head v → Eval (.op .succ args) (.succ v)
  /-- Pred. -/
  | pred {p : Bool} {args : Tuple (Closed p) [.nat]} {v : Closed p .nat} :
      Eval args.head (.succ v) → Eval (.op .pred args) v
  /-- ZeroZ. -/
  | isZeroZ {p : Bool} {args : Tuple (Closed p) [.nat]} :
      Eval args.head .zero → Eval (.op .isZero args) .tt
  /-- ZeroS. -/
  | isZeroS {p : Bool} {args : Tuple (Closed p) [.nat]} {v : Closed p .nat} :
      Eval args.head (.succ v) → Eval (.op .isZero args) .ff
  /-- IfT. -/
  | iteT {p : Bool} {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} {v : Closed p τ} :
      Eval args.head .tt → Eval args.tail.head v → Eval (.op (.ite τ) args) v
  /-- IfF. -/
  | iteF {p : Bool} {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} {v : Closed p τ} :
      Eval args.head .ff → Eval args.tail.tail.head v → Eval (.op (.ite τ) args) v
  /-- Fun (call-by-name application). -/
  | app {p : Bool} {σ τ : Ty} {t : Closed p (σ ⤳ τ)} {t' : Term p [σ] τ} {u : Closed p σ}
      {v : Closed p τ} :
      Eval t (.lam t') → Eval (t'.subst1 u) v → Eval (.app t u) v
  /-- Fix. -/
  | fix {p : Bool} {τ : Ty} {args : Tuple (Closed p) [τ ⤳ τ]} {v : Closed p τ} :
      Eval (.app args.head (.op (.fix τ) args)) v → Eval (.op (.fix τ) args) v
  /-- PorL. -/
  | porL {args : Tuple (Closed true) [.bool, .bool]} :
      Eval args.head .tt → Eval (.op .por args) .tt
  /-- PorR. -/
  | porR {args : Tuple (Closed true) [.bool, .bool]} :
      Eval args.tail.head .tt → Eval (.op .por args) .tt
  /-- PorF. -/
  | porF {args : Tuple (Closed true) [.bool, .bool]} :
      Eval args.head .ff → Eval args.tail.head .ff → Eval (.op .por args) .ff

@[inherit_doc] infix:50 " ⇓ " => Eval

/-- `t` has no result (`t ⇑`). This is Isotope's `NoResult`, not an infinite reduction:
stuck terms such as `pred 𝟶` diverge in this sense. See `PCF.Divergence`. -/
def Diverges {p : Bool} {τ : Ty} (t : Closed p τ) : Prop := ∀ v, ¬ t ⇓ v

namespace Eval

variable {p : Bool} {τ : Ty}

theorem value_iff {v w : Closed p τ} (hv : Value v) : v ⇓ w ↔ w = v := sorry

/-! ### Inversion of evaluation -/

/-- `true ⇓ false` fails. -/
theorem not_tt_ff : ¬ (Term.tt : Closed p .bool) ⇓ .ff := sorry

end Eval

section Omega

variable {p : Bool}

end Omega

section Example20

variable {p : Bool}

/-- The value `fun x:nat. (fun y:nat. y) 𝟶` of Example 20: a β-redex under a
binder. -/
abbrev lamBetaZero (p : Bool) : Closed p (Ty.nat ⤳ Ty.nat) :=
  .lam (.app (.lam Term.v0) Term.zero)

/-- The value `fun x:nat. 𝟶` of Example 20, the contractum of `lamBetaZero`. -/
abbrev lamZero (p : Bool) : Closed p (Ty.nat ⤳ Ty.nat) := .lam Term.zero

end Example20

end Silmaril.Calculi.PCF
