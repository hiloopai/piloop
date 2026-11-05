module

public import Silmaril.Calculi.PCF.Morphism
public import Silmaril.Calculi.TypedLambda.Context

/-! # Contextual equivalence for PCF

§5.3 of the notes: complete programs are closed terms of type `nat` or `bool`,
and what is observed is the value they evaluate to, if any. Contextual
equivalence (Definition 21) and the contextual preorder (Definition 30) are
the generic notions of `Silmaril.Calculi.TypedLambda.Observation` for this observation.

The inclusion of PCF into PCF+`por` reflects the contextual preorder. The
larger language has more contexts, so it can only distinguish more programs.
§8 shows that it does distinguish more.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The observation of §5.3: programs of ground type, observed by evaluation. -/
def obs (p : Bool) : Observation (sig p) := ⟨Ty.IsGround, Eval⟩

variable {p : Bool} {Γ : Ctx Base} {τ : Ty}

/-- Definition 30: the contextual preorder `Γ ⊢ t ≤ctx t' : τ`. -/
abbrev CtxLe (t t' : Term p Γ τ) : Prop := (obs p).CtxLe t t'

/-- Definition 21: contextual equivalence `Γ ⊢ t ≅ctx t' : τ`. -/
abbrev CtxEquiv (t t' : Term p Γ τ) : Prop := (obs p).CtxEquiv t t'

@[inherit_doc] infix:50 " ≤ctx " => CtxLe
@[inherit_doc] infix:50 " ≅ctx " => CtxEquiv

/-- Translate a PCF context into a PCF+`por` context. -/
abbrev Context.embed {Δ : Ctx Base} {σ : Ty} (C : TypedLambda.Context (sig false) Δ σ Γ τ) :
    TypedLambda.Context (sig true) Δ σ Γ τ :=
  TypedLambda.Context.mapOp Op.lift C

theorem Context.embed_plug {Δ : Ctx Base} {σ : Ty} (C : TypedLambda.Context (sig false) Δ σ Γ τ)
    (t : Term false Δ σ) : Term.embed (C.plug t) = (Context.embed C).plug t.embed := sorry

/-- The inclusion of PCF into PCF+`por` reflects the contextual preorder. -/
theorem CtxLe.of_embed {t t' : Term false Γ τ} (h : t.embed ≤ctx t'.embed) : t ≤ctx t' := sorry

/-- The inclusion of PCF into PCF+`por` reflects contextual equivalence. -/
theorem CtxEquiv.of_embed {t t' : Term false Γ τ} (h : t.embed ≅ctx t'.embed) : t ≅ctx t' := sorry

end Silmaril.Calculi.PCF
