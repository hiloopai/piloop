module

public import Silmaril.Semantics.Domain.Flat
public import Silmaril.Semantics.Domain.Product
public import Silmaril.Semantics.Domain.Kleene

/-! # Function domains

Definition 15 of the notes (§3.3) is Mathlib's ω-cpo of continuous functions
`D →𝒄 E`, ordered pointwise with pointwise lubs. This module adds its least
element and bundles the operations the semantics of PCF needs: evaluation
(Proposition 13), currying (Proposition 14), composition (Proposition 15) and
the conditional `if : 𝔹⊥ × (D × D) → D`, which the notes cite as
Proposition 11 (the notes' numbering skips it). Continuity of `fix`
(Proposition 16) is `Silmaril.Semantics.Domain.fixHom`. The least fixed point of a
family of maps acting componentwise on a product is computed componentwise
(`fix_piMap`).
-/

@[expose] public section

namespace Silmaril.Semantics.Domain

open OmegaCompletePartialOrder

variable {D D' E F : Type*} [OmegaCompletePartialOrder D] [OmegaCompletePartialOrder D']
  [OmegaCompletePartialOrder E] [OmegaCompletePartialOrder F]

/-- Definition 15: the least continuous function is constantly `⊥`. -/
instance [OrderBot E] : OrderBot (D →𝒄 E) where
  bot := ContinuousHom.const ⊥
  bot_le _ _ := bot_le

/-- The least continuous map lies over Mathlib's least monotone map. -/
theorem ContinuousHom.toMono_bot [OrderBot E] : (⊥ : D →𝒄 E).toMono = ⊥ := sorry

/-- Definition 15: lubs of continuous functions are computed argumentwise. -/
theorem continuousHom_ωSup_apply (c : Chain (D →𝒄 E)) (d : D) :
    ωSup c d = ωSup (c.map ⟨fun f : D →𝒄 E => f d, fun _ _ h => h d⟩) := sorry

/-- A map into a function cpo is continuous if it is continuous at each argument. -/
theorem ωScottContinuous_continuousHom {g : D' → D →𝒄 E}
    (h : ∀ d, ωScottContinuous fun d' => g d' d) : ωScottContinuous g := sorry

/-- Proposition 13: evaluation is continuous (Mathlib `ContinuousHom.Prod.apply`). -/
def evalHom' : (D →𝒄 E) × D →𝒄 E := ContinuousHom.Prod.apply

@[simp] theorem evalHom'_apply (f : D →𝒄 E) (d : D) : evalHom' (f, d) = f d := sorry

/-- Proposition 14: the curried form of a continuous map. -/
def curry (f : D' × D →𝒄 E) : D' →𝒄 D →𝒄 E :=
  ContinuousHom.ofFun
    (fun d' => ContinuousHom.ofFun (fun d => f (d', d))
      (f.ωScottContinuous.comp (Prod.ωScottContinuous.prodMk ωScottContinuous.const
        ωScottContinuous.id)))
    (ωScottContinuous_continuousHom fun _ =>
      f.ωScottContinuous.comp (Prod.ωScottContinuous.prodMk ωScottContinuous.id
        ωScottContinuous.const))

/-- The uncurried form of a continuous map into a function cpo. -/
def uncurry (g : D' →𝒄 D →𝒄 E) : D' × D →𝒄 E :=
  evalHom'.comp (pairHom (g.comp fstHom) sndHom)

@[simp] theorem uncurry_apply (g : D' →𝒄 D →𝒄 E) (p : D' × D) : uncurry g p = g p.1 p.2 := sorry

@[simp] theorem uncurry_curry (f : D' × D →𝒄 E) : uncurry (curry f) = f := sorry

@[simp] theorem curry_uncurry (g : D' →𝒄 D →𝒄 E) : curry (uncurry g) = g := sorry

/-- Proposition 14: currying is itself continuous. -/
def curryHom : (D' × D →𝒄 E) →𝒄 D' →𝒄 D →𝒄 E :=
  ContinuousHom.ofFun curry <|
    ωScottContinuous_continuousHom fun _ =>
      ωScottContinuous_continuousHom fun _ =>
        ContinuousHom.ωScottContinuous_apply ωScottContinuous.id ωScottContinuous.const

/-- Proposition 15: composition `(f, g) ↦ f ∘ g` is continuous. The notes
write the composite as `λd. g(f(d))`, which does not typecheck for
`f : E → F` and `g : D → E`; the intended map is `λd. f(g(d))`. -/
def compHom : (E →𝒄 F) × (D →𝒄 E) →𝒄 D →𝒄 F :=
  ContinuousHom.ofFun (fun p => p.1.comp p.2) <|
    ωScottContinuous_continuousHom fun _ =>
      ContinuousHom.ωScottContinuous_apply Prod.ωScottContinuous_fst
        (ContinuousHom.ωScottContinuous_apply Prod.ωScottContinuous_snd ωScottContinuous.const)

@[simp] theorem compHom_apply (f : E →𝒄 F) (g : D →𝒄 E) : compHom (f, g) = f.comp g := sorry

section Conditional

variable [OrderBot D]

open Classical in
/-- The semantic conditional: `if(true, x, y) = x`, `if(false, x, y) = y` and
`if(⊥, x, y) = ⊥`. -/
noncomputable def condFun (b : Part Bool) (x y : D) : D :=
  if h : b.Dom then (if b.get h then x else y) else ⊥

@[simp] theorem condFun_true (x y : D) : condFun (Part.some true) x y = x := sorry

@[simp] theorem condFun_false (x y : D) : condFun (Part.some false) x y = y := sorry

@[simp] theorem condFun_none (x y : D) : condFun Part.none x y = ⊥ := sorry

/-- Proposition 11: the conditional is continuous. -/
theorem ωScottContinuous_condFun :
    ωScottContinuous fun p : Part Bool × (D × D) => condFun p.1 p.2.1 p.2.2 := sorry

/-- Proposition 11: the conditional as a continuous map. -/
noncomputable def cond : Part Bool × (D × D) →𝒄 D :=
  ContinuousHom.ofFun _ ωScottContinuous_condFun

@[simp] theorem cond_apply (b : Part Bool) (x y : D) : cond (b, x, y) = condFun b x y := sorry

end Conditional

section PiFix

variable {I : Type*} {P : I → Type*} [∀ i, OmegaCompletePartialOrder (P i)]

/-- A family of continuous maps `P i → P i`, acting componentwise on the
product `∀ i, P i` (Proposition 12). -/
def piMap (f : ∀ i, P i →𝒄 P i) : (∀ i, P i) →𝒄 ∀ i, P i :=
  piHom fun i => (f i).comp (evalHom i)

@[simp] theorem piMap_apply (f : ∀ i, P i →𝒄 P i) (d : ∀ i, P i) (i : I) :
    piMap f d i = f i (d i) := sorry

end PiFix

end Silmaril.Semantics.Domain
