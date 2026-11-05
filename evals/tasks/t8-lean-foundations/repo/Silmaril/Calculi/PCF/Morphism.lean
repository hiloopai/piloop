module

public import Silmaril.Calculi.PCF.BigStep
public import Silmaril.Calculi.TypedLambda.Morphism

/-! # Morphisms between PCF and PCF+`por`

`Term.embed` includes PCF into PCF+`por` (an operator translation).
`Term.forget` goes back, replacing parallel or by the left sequential or
`if t then true else u` of Definition 33 (a derived-operator translation). On
the other operators it is the identity. `forget` is a retraction of `embed`
and inverts it on `por`-free terms.

Both translations preserve evaluation of `por`-free terms. Consequently
PCF+`por` is a conservative extension of PCF (`Eval.of_embed`): an embedded
PCF program evaluates in PCF+`por` exactly as it does in PCF. Contextual
equivalence is a different matter: parallel or distinguishes PCF programs
that PCF cannot (§8).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

namespace Op

/-- Include the operators of PCF, or of PCF+`por`, into those of PCF+`por`. -/
def lift {p : Bool} : {as : List Ty} → {τ : Ty} → Op p as τ → Op true as τ
  | _, _, .por => .por
  | _, _, .zero => .zero
  | _, _, .succ => .succ
  | _, _, .pred => .pred
  | _, _, .tt => .tt
  | _, _, .ff => .ff
  | _, _, .isZero => .isZero
  | _, _, .ite τ => .ite τ
  | _, _, .fix τ => .fix τ

/-- Translate an operator into a PCF term over its argument variables:
`por(x, y)` becomes the left sequential or `if x then true else y`, and every
other operator is applied to its variables. -/
def forget {p : Bool} : {as : List Ty} → {τ : Ty} → Op p as τ → TypedLambda.Term (sig false) as τ
  | _, _, .zero => .op .zero (Subst.id _)
  | _, _, .succ => .op .succ (Subst.id _)
  | _, _, .pred => .op .pred (Subst.id _)
  | _, _, .tt => .op .tt (Subst.id _)
  | _, _, .ff => .op .ff (Subst.id _)
  | _, _, .isZero => .op .isZero (Subst.id _)
  | _, _, .ite τ => .op (.ite τ) (Subst.id _)
  | _, _, .fix τ => .op (.fix τ) (Subst.id _)
  | _, _, .por => Term.ite (.var .zero) Term.tt (.var (.succ .zero))

/-- The operators of PCF+`por` other than `por`. -/
def IsPCF {p : Bool} : {as : List Ty} → {τ : Ty} → Op p as τ → Prop
  | _, _, .por => False
  | _, _, _ => True

theorem forget_lift {as : List Ty} {τ : Ty} (o : Op false as τ) :
    (lift o).forget = .op o (Subst.id _) := sorry

theorem isPCF_lift {as : List Ty} {τ : Ty} (o : Op false as τ) : (lift o).IsPCF := sorry

theorem lift_forget {as : List Ty} {τ : Ty} (o : Op true as τ) (h : o.IsPCF) :
    ∃ o' : Op false as τ, lift o' = o ∧ o.forget = .op o' (Subst.id _) := sorry

end Op

namespace Term

variable {p : Bool} {Γ : Ctx Base} {σ τ : Ty}

/-- The inclusion into PCF+`por`; the identity on PCF+`por` itself. -/
abbrev lift : Term p Γ τ → Term true Γ τ := TypedLambda.Term.mapOp Op.lift

/-- The inclusion of PCF into PCF+`por`. -/
abbrev embed : Term false Γ τ → Term true Γ τ := lift

/-- Replace parallel or by sequential or. -/
abbrev forget : Term p Γ τ → Term false Γ τ := TypedLambda.Term.translate Op.forget

/-- A term with no occurrence of `por`. -/
def NoPor : {Γ : Ctx Base} → {τ : Ty} → Term p Γ τ → Prop
  | _, _, .var _ => True
  | _, _, .lam t => NoPor t
  | _, _, .app t u => NoPor t ∧ NoPor u
  | _, _, .op o args => o.IsPCF ∧ ∀ a x, NoPor (args a x)

theorem noPor_rename : {Γ Δ : Ctx Base} → {τ : Ty} → (ρ : Ren Γ Δ) → {t : Term p Γ τ} →
    NoPor t → NoPor (t.rename ρ) := sorry

theorem noPor_lift {Γ Δ : Ctx Base} {s : Subst (sig p) Γ Δ} (hs : ∀ a x, NoPor (s a x)) :
    ∀ a x, NoPor (Subst.lift (σ := σ) s a x) := sorry

section SmartConstructors

variable {Γ : Ctx Base}

end SmartConstructors

end Term

namespace Value

variable {p : Bool} {τ : Ty}

theorem embed {v : Closed false τ} (h : Value v) : Value v.embed := sorry

end Value

namespace Eval

variable {p : Bool} {τ : Ty}

end Eval

end Silmaril.Calculi.PCF
