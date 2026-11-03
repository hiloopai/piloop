module

public import Mathlib.Data.Set.Basic

/-! # §17 Effect systems (notes pp. 30–31; slides pp. 108–112)

The language (notes p. 30)

  `e ::= x | λx.e | e₁ e₂ | ξ?x.e | ξ!e₁.e₂ | if e₁ then e₂ else e₃`

(plus integer constants), where `ξ?x.e` reads an integer from channel `ξ` and
`ξ!e₁.e₂` writes the value of `e₁` to `ξ`. Effects `F` are sets of `Rξ` and
`Wξ`, and function types carry their *latent* effect, `t →ᶠ t′`. The rules
(VAR), (READ), (WRITE), (LAM), (APP) are those of notes p. 31; (SUB) is the
subeffecting rule of the slides (p. 111), letting an expression with fewer
effects be used where more are allowed.

Safety: the effects of every evaluation are among the inferred effects
(`Derives.sound`). The semantics is call-by-value big-step evaluation of
closed terms by substitution, recording the channel events in order; a read
may receive any integer.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.EffectSystems

/-- Channels. -/
abbrev Chan := String

/-- Atomic effects: `Rξ` and `Wξ`. -/
inductive Eff where
  | read (ξ : Chan)
  | write (ξ : Chan)
  deriving DecidableEq

/-- Types `t ::= int | t →ᶠ t′`. -/
inductive Ty where
  | int
  | arrow (a : Ty) (F : Set Eff) (b : Ty)

/-- Terms. -/
inductive Term where
  | var (x : String)
  | lit (n : ℤ)
  | lam (x : String) (e : Term)
  | app (f a : Term)
  | recv (ξ : Chan) (x : String) (e : Term)
  | send (ξ : Chan) (e₁ e₂ : Term)
  | ite (c t e : Term)

/-- Contexts. -/
abbrev Ctx := String → Option Ty

/-- `Γ ⊢ e : t, F` (notes p. 31; (SUB) from the slides). -/
inductive Derives : Ctx → Term → Ty → Set Eff → Prop where
  | var {Γ x t} : Γ x = some t → Derives Γ (.var x) t ∅
  | lit {Γ n} : Derives Γ (.lit n) .int ∅
  | recv {Γ ξ x e t F} : Derives (Function.update Γ x (some .int)) e t F →
      Derives Γ (.recv ξ x e) t ({.read ξ} ∪ F)
  | send {Γ ξ e₁ e₂ t F F'} : Derives Γ e₁ .int F → Derives Γ e₂ t F' →
      Derives Γ (.send ξ e₁ e₂) t (F ∪ {.write ξ} ∪ F')
  | lam {Γ x e t t' F} : Derives (Function.update Γ x (some t)) e t' F →
      Derives Γ (.lam x e) (.arrow t F t') ∅
  | app {Γ f a t t' F F' F''} : Derives Γ f (.arrow t F'' t') F → Derives Γ a t F' →
      Derives Γ (.app f a) t' (F ∪ F' ∪ F'')
  | ite {Γ c e₁ e₂ t F F₁ F₂} : Derives Γ c .int F → Derives Γ e₁ t F₁ → Derives Γ e₂ t F₂ →
      Derives Γ (.ite c e₁ e₂) t (F ∪ F₁ ∪ F₂)
  | sub {Γ e t F F'} : Derives Γ e t F → F ⊆ F' → Derives Γ e t F'

namespace Term

/-- Substitution of a closed term for a variable (no capture is possible). -/
def subst (x : String) (v : Term) : Term → Term
  | var y => if y = x then v else var y
  | lit n => lit n
  | lam y e => if y = x then lam y e else lam y (subst x v e)
  | app f a => app (subst x v f) (subst x v a)
  | recv ξ y e => if y = x then recv ξ y e else recv ξ y (subst x v e)
  | send ξ e₁ e₂ => send ξ (subst x v e₁) (subst x v e₂)
  | ite c t e => ite (subst x v c) (subst x v t) (subst x v e)

end Term

/-- Call-by-value evaluation `e ⇓ v` with the channel events performed. -/
inductive Eval : Term → List Eff → Term → Prop where
  | lit {n} : Eval (.lit n) [] (.lit n)
  | lam {x e} : Eval (.lam x e) [] (.lam x e)
  -- Substitute the argument value into the function body: `body[x := v]`.
  | app {f a x body v r ef ea eb} : Eval f ef (.lam x body) → Eval a ea v →
      Eval (Term.subst x v body) eb r → Eval (.app f a) (ef ++ ea ++ eb) r
  -- Substitute the received value into the continuation: `e[x := n]`.
  | recv {ξ x e n w r} : Eval (Term.subst x (.lit n) e) w r → Eval (.recv ξ x e) (.read ξ :: w) r
  | send {ξ e₁ e₂ n w₁ w₂ r} : Eval e₁ w₁ (.lit n) → Eval e₂ w₂ r →
      Eval (.send ξ e₁ e₂) (w₁ ++ .write ξ :: w₂) r
  | iteT {c e₁ e₂ n w w₁ r} : Eval c w (.lit n) → n ≠ 0 → Eval e₁ w₁ r →
      Eval (.ite c e₁ e₂) (w ++ w₁) r
  | iteF {c e₁ e₂ w w₂ r} : Eval c w (.lit 0) → Eval e₂ w₂ r → Eval (.ite c e₁ e₂) (w ++ w₂) r

namespace Derives

end Derives

theorem Derives.sound {e : Term} {t : Ty} {F : Set Eff} (hd : Derives (fun _ => none) e t F)
    {w : List Eff} {v : Term} (he : Eval e w v) : ∀ ε ∈ w, ε ∈ F := sorry

/-- The notes' example (p. 31): `ξ!1.λx.ζ!2.x` has immediate effect `{Wξ}` and
latent effect `{Wζ}`. -/
theorem notesExample :
    Derives (fun _ => none) (.send "ξ" (.lit 1) (.lam "x" (.send "ζ" (.lit 2) (.var "x"))))
      (.arrow .int {.write "ζ"} .int) {.write "ξ"} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.EffectSystems
