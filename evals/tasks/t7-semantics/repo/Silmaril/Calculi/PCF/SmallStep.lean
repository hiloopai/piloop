module

public import Silmaril.Calculi.PCF.BigStep
public import Cslib.Foundations.Relation.Termination
public import Cslib.Foundations.Relation.Attr
public import Cslib.Foundations.Relation.Confluence

/-! # Transitions

The small-step transition relation `t ⇝ t'` of Figure 4 of the notes
(Remark 7), extended to PCF+`por` as asked by Exercise 22. The relation is
registered with CSLib's `reduction_sys` attribute, so `t ⭢ t'` is a step and
`t ↠ t'` is its reflexive-transitive closure (`Relation.ReflTransGen`).

For parallel or, either argument may take a step, and a result is produced as
soon as one argument is `true` or both are `false`. The main theorem is
Remark 7 for both languages: `t ⇓ v ↔ t ↠ v` for every value `v`
(`eval_iff_steps`). Values are CSLib normal forms. Transitions of PCF are
deterministic, while those of PCF+`por` are not.

As for evaluation, rules about operators quantify over the argument tuple;
nested patterns such as `pred(succ(v))` become equations on the arguments.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- Figure 4, with transitions for parallel or (Exercise 22). -/
@[reduction_sys]
inductive Step : {p : Bool} → {τ : Ty} → Closed p τ → Closed p τ → Prop
  /-- Reduce the argument of `succ`. -/
  | succ {p : Bool} {args : Tuple (Closed p) [.nat]} {t' : Closed p .nat} :
      Step args.head t' → Step (.op .succ args) (.succ t')
  /-- Reduce the argument of `pred`. -/
  | pred {p : Bool} {args : Tuple (Closed p) [.nat]} {t' : Closed p .nat} :
      Step args.head t' → Step (.op .pred args) (.pred t')
  /-- Reduce the argument of `zero?`. -/
  | isZero {p : Bool} {args : Tuple (Closed p) [.nat]} {t' : Closed p .nat} :
      Step args.head t' → Step (.op .isZero args) (.isZero t')
  /-- `pred(succ(v)) ⇝ v`. -/
  | predSucc {p : Bool} {args : Tuple (Closed p) [.nat]} {v : Closed p .nat} :
      args.head = .succ v → Value v → Step (.op .pred args) v
  /-- `zero?(𝟶) ⇝ true`. -/
  | isZeroZero {p : Bool} {args : Tuple (Closed p) [.nat]} :
      args.head = .zero → Step (.op .isZero args) .tt
  /-- `zero?(succ(v)) ⇝ false`. -/
  | isZeroSucc {p : Bool} {args : Tuple (Closed p) [.nat]} {v : Closed p .nat} :
      args.head = .succ v → Value v → Step (.op .isZero args) .ff
  /-- Reduce the condition of `if`. -/
  | ite {p : Bool} {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} {b' : Closed p .bool} :
      Step args.head b' → Step (.op (.ite τ) args) (.op (.ite τ) (b' ::ₜ args.tail))
  /-- `if true then t₁ else t₂ ⇝ t₁`. -/
  | iteT {p : Bool} {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} :
      args.head = .tt → Step (.op (.ite τ) args) args.tail.head
  /-- `if false then t₁ else t₂ ⇝ t₂`. -/
  | iteF {p : Bool} {τ : Ty} {args : Tuple (Closed p) [.bool, τ, τ]} :
      args.head = .ff → Step (.op (.ite τ) args) args.tail.tail.head
  /-- Reduce the function of an application. -/
  | app {p : Bool} {σ τ : Ty} {t t' : Closed p (σ ⤳ τ)} {u : Closed p σ} :
      Step t t' → Step (.app t u) (.app t' u)
  /-- `(fun x:σ. t) u ⇝ t[u/x]`. -/
  | beta {p : Bool} {σ τ : Ty} {t : Term p [σ] τ} {u : Closed p σ} :
      Step (.app (.lam t) u) (t.subst1 u)
  /-- `fix(t) ⇝ t (fix(t))`. -/
  | fix {p : Bool} {τ : Ty} {args : Tuple (Closed p) [τ ⤳ τ]} :
      Step (.op (.fix τ) args) (.app args.head (.op (.fix τ) args))
  /-- Reduce the left argument of `por`. -/
  | porL {args : Tuple (Closed true) [.bool, .bool]} {t' : Closed true .bool} :
      Step args.head t' → Step (.op .por args) (.op .por (t' ::ₜ args.tail))
  /-- Reduce the right argument of `por`. -/
  | porR {args : Tuple (Closed true) [.bool, .bool]} {u' : Closed true .bool} :
      Step args.tail.head u' → Step (.op .por args) (.op .por (args.head ::ₜ .single u'))
  /-- `por(true, u) ⇝ true`. -/
  | porTL {args : Tuple (Closed true) [.bool, .bool]} :
      args.head = .tt → Step (.op .por args) .tt
  /-- `por(t, true) ⇝ true`. -/
  | porTR {args : Tuple (Closed true) [.bool, .bool]} :
      args.tail.head = .tt → Step (.op .por args) .tt
  /-- `por(false, false) ⇝ false`. -/
  | porFF {args : Tuple (Closed true) [.bool, .bool]} :
      args.head = .ff → args.tail.head = .ff → Step (.op .por args) .ff

namespace Step

variable {p : Bool} {τ : Ty}

end Step

namespace Step

variable {τ : Ty}

/-- Transitions of PCF are confluent in CSLib's sense, derived from determinism
through CSLib's `Relation.RightUnique.to_confluent`. -/
theorem confluent : Relation.Confluent (Step (p := false) (τ := τ)) := sorry

end Step

end Silmaril.Calculi.PCF
