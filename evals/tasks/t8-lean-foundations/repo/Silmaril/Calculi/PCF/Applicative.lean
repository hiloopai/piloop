module

public import Silmaril.Calculi.PCF.Extensionality
public import Silmaril.Calculi.TypedLambda.Applicative

/-! # Applicative bisimilarity for PCF

§8.2 of the notes (p. 69) mentions Abramsky's applicative bisimulation as an
operational characterisation of contextual equivalence. Applicative
similarity and bisimilarity are the generic notions of
`Silmaril.Calculi.TypedLambda.Observation` for the observation of §5.3 (`PCF.obs`). Both
PCF and PCF+`por` are covered.

Every base type is observed, and by Proposition 39 the contextual preorder is
inclusion of results at ground types (`ctxLe_iff_eval`) and is tested on
closed arguments at function types (`ctxLe_arrow_iff`). So on closed terms
applicative similarity is the contextual preorder (`appSim_iff_ctxLe`) and
applicative bisimilarity is contextual equivalence (`appBisim_iff_ctxEquiv`).
Through the generic correspondence they are also CSLib similarity and
bisimilarity of the applicative transition system
(`ctxLe_iff_similarity`, `ctxEquiv_iff_bisimilarity`).

Only closed terms are compared. The extension to open terms (relating `t` and
`t'` when all their closing substitutions are related) is the usual next step
but is not formalized here.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

variable {p : Bool} {τ : Ty}

/-- Applicative similarity of closed PCF terms, `t ≲ t'`. -/
abbrev AppSim (t t' : Closed p τ) : Prop := (obs p).AppSim τ t t'

/-- Applicative bisimilarity of closed PCF terms. -/
abbrev AppBisim (t t' : Closed p τ) : Prop := (obs p).AppBisim τ t t'

open Cslib.LTS in
/-- On closed PCF terms, the contextual preorder is CSLib similarity in the
applicative transition system. -/
theorem ctxLe_iff_similarity {t t' : Closed p τ} :
    t ≤ctx t' ↔ Similarity (obs p).appLTS (obs p).appLTS (.term τ t) (.term τ t') := sorry

open Cslib.LTS in
/-- On closed PCF terms, contextual equivalence is CSLib bisimilarity in the
applicative transition system. -/
theorem ctxEquiv_iff_bisimilarity {t t' : Closed p τ} :
    t ≅ctx t' ↔ Bisimilarity (obs p).appLTS (obs p).appLTS (.term τ t) (.term τ t') := sorry

end Silmaril.Calculi.PCF
