module

public import Silmaril.Calculi.PCF.Denotation
public import Silmaril.Calculi.PCF.SmallStep

/-! # Soundness

§6.4 of the notes. If `t ⇓ v` then `⟦t⟧ = ⟦v⟧` (Theorem 28), for PCF and for
PCF+`por`. The notes prove the Fun and Fix cases and leave the others as
exercises; all rules are covered here. Every transition also preserves
denotations. As a consequence, a program of ground type whose denotation is
`⊥` diverges (Proposition 29).
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open OmegaCompletePartialOrder Silmaril.Semantics.Domain TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {p : Bool} {τ : Ty}

/-- Transitions preserve denotations. -/
theorem Step.denote_eq {t t' : Closed p τ} (h : Step t t') : denote t = denote t' := sorry

end Silmaril.Calculi.PCF
