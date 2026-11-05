module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Observational
public import Silmaril.Compile.PassAPI.Pass

@[expose] public section

/-! # Course transformations as shared verified passes

The contract preserves exactly the course's behavior sets for every initial
state. It has no additional program invariant. This adapter reuses the
existing observational correctness proofs; it does not assert a connection
to a machine-code backend or change the course's observation model.
-/

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions

open Silmaril.Compile.PassAPI

/-- Accept replacements with identical course behavior for every initial state. -/
def observationalContract (V : Type) [DecidableEq V] : Contract (Prog V) where
  Rel P Q := ∀ s, P.beh s = Q.beh s
  refl _ _ := rfl
  trans hPQ hQR s := (hPQ s).trans (hQR s)
  Inv _ := True

/-- Any proved sound course transformation is a shared verified pass. -/
def Transformation.toPass {V : Type} [DecidableEq V] (T : Transformation V) (hT : T.Sound) :
    Pass (observationalContract V) where
  run := T
  inv_run _ := trivial
  sound _ s := (hT.beh_eq _ s).symm

/-- A natural family supplies a pass at each register signature, in addition
to its existing institution comorphism. Naturality is not required for a single pass. -/
def SoundFamily.toPass (F : SoundFamily) (A : RegSig) : Pass (observationalContract A.V) :=
  (F.T A).toPass (F.sound A)

/-- Liveness-driven DCE, packaged with the shared pass contract. -/
def dcePass {V : Type} [DecidableEq V] : Pass (observationalContract V) :=
  dce.toPass dce_sound

@[simp] theorem dcePass_run {V : Type} [DecidableEq V] (P : Prog V) :
    dcePass.run P = dce P := sorry

/-- The pass guarantee is exactly the existing observational transformation guarantee. -/
theorem toPass_beh {V : Type} [DecidableEq V] (T : Transformation V) (hT : T.Sound)
    (P : Prog V) (s : State V) : ((T.toPass hT).run P).beh s = P.beh s := sorry

/-- Shared pipeline composition preserves course behavior for every initial state. -/
theorem pipeline_beh {V : Type} [DecidableEq V]
    (ps : List (Pass (observationalContract V))) (P : Prog V) (s : State V) :
    ((Pass.pipeline ps).run P).beh s = P.beh s := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions
