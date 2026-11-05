module

public import Silmaril.Calculi.TypedLambda.Morphism
public import Cslib.Foundations.Syntax.Congruence

/-! # Contexts and contextual equivalence

A context `C : Context S Δ σ Γ τ` is a term of type `τ` in `Γ` with a single
hole, which expects a term of type `σ` in `Δ` (Definitions 19–20 of the
notes, where it is written `Γ ⊢_{Δ,σ} C : τ`). Plugging (`C[t]`) may capture
variables, since the hole may sit under binders. It is CSLib's `HasHContext`
filling operation, written `C<[t]`.

Contextual equivalence (Definition 21) and the contextual preorder
(Definition 30) are defined relative to an `Observation`: a class of ground
result types and an evaluation relation on closed terms. A complete program
is a closed term of ground type. Two terms are equivalent if every context
that makes them complete programs yields the same results. Contextual
equivalence is a CSLib `LawfulCongruence`.

The numbered definitions refer to the lecture notes.
The observations are supplied parameters; this file does not choose an
operational semantics for an arbitrary signature.
-/

@[expose] public section

namespace Silmaril.Calculi.TypedLambda

open Cslib.LambdaCalculus.LocallyNameless.Stlc

variable (S : Signature)

/-- One-hole contexts: `Context S Δ σ Γ τ` has a hole of type `σ` in context `Δ`
and itself has type `τ` in context `Γ`. -/
inductive Context (Δ : Ctx S.Base) (σ : Ty S.Base) : Ctx S.Base → Ty S.Base → Type
  /-- The hole `−`. -/
  | hole : Context Δ σ Δ σ
  /-- `fun x:α. C`. -/
  | lam {Γ : Ctx S.Base} {α τ : Ty S.Base} : Context Δ σ (α :: Γ) τ → Context Δ σ Γ (α ⤳ τ)
  /-- `C u`. -/
  | appL {Γ : Ctx S.Base} {α τ : Ty S.Base} :
      Context Δ σ Γ (α ⤳ τ) → Term S Γ α → Context Δ σ Γ τ
  /-- `t C`. -/
  | appR {Γ : Ctx S.Base} {α τ : Ty S.Base} :
      Term S Γ (α ⤳ τ) → Context Δ σ Γ α → Context Δ σ Γ τ
  /-- An operator with the hole in argument `x`; the argument of `args` at `x`
  is ignored. -/
  | op {Γ : Ctx S.Base} {as : List (Ty S.Base)} {α τ : Ty S.Base} :
      S.Op as τ → Tuple (Term S Γ) as → Var as α → Context Δ σ Γ α → Context Δ σ Γ τ

variable {S}

namespace Context

variable {Δ Δ' Γ : Ctx S.Base} {σ σ' τ : Ty S.Base}

/-- Fill the hole, `C[t]`. Free variables of `t` may be captured. -/
def plug : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ → Term S Δ σ → Term S Γ τ
  | _, _, hole, t => t
  | _, _, lam C, t => .lam (C.plug t)
  | _, _, appL C u, t => .app (C.plug t) u
  | _, _, appR f C, t => .app f (C.plug t)
  | _, _, op o args x C, t => .op o (args.set x (C.plug t))

/-- Compose contexts: plug the context `C'` into the hole of `C`. -/
def comp : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ' σ' Γ τ → Context S Δ σ Δ' σ' →
    Context S Δ σ Γ τ
  | _, _, hole, C' => C'
  | _, _, lam C, C' => lam (C.comp C')
  | _, _, appL C u, C' => appL (C.comp C') u
  | _, _, appR f C, C' => appR f (C.comp C')
  | _, _, op o args x C, C' => op o args x (C.comp C')

theorem plug_comp (C : Context S Δ' σ' Γ τ) (C' : Context S Δ σ Δ' σ') (t : Term S Δ σ) :
    (C.comp C').plug t = C.plug (C'.plug t) := sorry

/-- Translate the operators of a context. -/
def mapOp {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op') :
    {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ → Context (S.withOps Op') Δ σ Γ τ
  | _, _, hole => hole
  | _, _, lam C => lam (C.mapOp f)
  | _, _, appL C u => appL (C.mapOp f) (u.mapOp f)
  | _, _, appR g C => appR (g.mapOp f) (C.mapOp f)
  | _, _, op o args x C => op (f o) (fun _ y => (args _ y).mapOp f) x (C.mapOp f)

/-! ### Contexts whose hole is not under a binder

A context `C : Context S Γ σ Γ τ` has its hole in its own context `Γ`, so no
binder lies on the path to the hole. These are the contexts of closed terms,
`Context S [] σ [] τ`. Substituting into all of their subterms commutes with
plugging, and a closed one can be weakened under a new binder and closed off
into a function `fun x:σ. C[x]` (`Context.close`).

Substitution into a context with binders above its hole would lift the
substitution under each binder and change the hole context by the binders on
the path, which is not definitionally the target context even when there are
none; the restriction keeps the hole context of `C.subst h s` equal to the
target context, which is what closing a context needs. -/

/-- The context of the hole extends the context of the whole. -/
theorem length_le : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ →
    Γ.length ≤ Δ.length := sorry

/-- A context whose own context is `α :: Γ` does not have its hole in `Γ`: the
hole context extends the context of the whole. (Used to rule out a `lam` above
the hole of a context whose hole is in the context of the whole.) -/
theorem ne_of_cons {α : Ty S.Base} (C : Context S Δ σ (α :: Γ) τ) : Δ ≠ Γ := sorry

/-- Apply a substitution to every subterm of a context whose hole is in the
context of the whole (`h : Δ = Γ`). -/
def subst {Θ : Ctx S.Base} : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ →
    Δ = Γ → Subst S Γ Θ → Context S Θ σ Θ τ
  | _, _, hole, _, _ => hole
  | _, _, lam C, h, _ => absurd h C.ne_of_cons
  | _, _, appL C u, h, s => appL (C.subst h s) (u.subst s)
  | _, _, appR f C, h, s => appR (f.subst s) (C.subst h s)
  | _, _, op o args x C, h, s => op o (fun _ y => (args _ y).subst s) x (C.subst h s)

/-- Weaken a closed context into an arbitrary context `Γ`. -/
def ofClosed (C : Context S [] σ [] τ) : Context S Γ σ Γ τ := C.subst rfl fun _ x => x.elim

/-- The closed function `fun x:σ. C[x]` of a closed context. -/
def close (C : Context S [] σ [] τ) : Term S [] (σ ⤳ τ) :=
  .lam ((ofClosed (Γ := [σ]) C).plug (.var .zero))

/-- CSLib's notation `C<[t]` for plugging. -/
instance : Cslib.HasHContext (Term S Γ τ) (Term S Δ σ) where
  Context := Context S Δ σ Γ τ
  fill := plug

theorem fill_eq_plug (C : Context S Δ σ Γ τ) (t : Term S Δ σ) :
    Cslib.HasHContext.fill (α := Term S Γ τ) C t = C.plug t := sorry

end Context

/-- What can be observed of a complete program: a class of ground types and an
evaluation relation on closed terms. -/
structure Observation (S : Signature) where
  /-- The types of complete programs. -/
  Ground : Ty S.Base → Prop
  /-- The observable results of evaluation. -/
  Eval : {τ : Ty S.Base} → Term S [] τ → Term S [] τ → Prop

namespace Observation

variable (O : Observation S) {Γ : Ctx S.Base} {τ : Ty S.Base}

/-- Definition 30: the contextual preorder `Γ ⊢ t ≤ctx t' : τ`. Every result of
a complete program built around `t` is also a result of the same program built
around `t'`. -/
def CtxLe (t t' : Term S Γ τ) : Prop :=
  ∀ γ, O.Ground γ → ∀ (C : Context S Γ τ [] γ) (v : Term S [] γ),
    O.Eval (C.plug t) v → O.Eval (C.plug t') v

/-- Definition 21: contextual equivalence `Γ ⊢ t ≅ctx t' : τ`. -/
def CtxEquiv (t t' : Term S Γ τ) : Prop :=
  ∀ γ, O.Ground γ → ∀ (C : Context S Γ τ [] γ) (v : Term S [] γ),
    O.Eval (C.plug t) v ↔ O.Eval (C.plug t') v

variable {O}

theorem ctxEquiv_iff_le {t t' : Term S Γ τ} : O.CtxEquiv t t' ↔ O.CtxLe t t' ∧ O.CtxLe t' t := sorry

theorem CtxLe.refl (t : Term S Γ τ) : O.CtxLe t t := sorry

theorem CtxLe.trans {t₁ t₂ t₃ : Term S Γ τ} (h₁ : O.CtxLe t₁ t₂) (h₂ : O.CtxLe t₂ t₃) :
    O.CtxLe t₁ t₃ := sorry

/-- The contextual preorder is preserved by contexts. -/
theorem CtxLe.plug {Δ : Ctx S.Base} {σ : Ty S.Base} {t t' : Term S Δ σ} (h : O.CtxLe t t')
    (C : Context S Δ σ Γ τ) : O.CtxLe (C.plug t) (C.plug t') := sorry

theorem CtxEquiv.refl (t : Term S Γ τ) : O.CtxEquiv t t := sorry

theorem CtxEquiv.symm {t t' : Term S Γ τ} (h : O.CtxEquiv t t') : O.CtxEquiv t' t := sorry

theorem CtxEquiv.trans {t₁ t₂ t₃ : Term S Γ τ} (h₁ : O.CtxEquiv t₁ t₂) (h₂ : O.CtxEquiv t₂ t₃) :
    O.CtxEquiv t₁ t₃ := sorry

/-- Contextual equivalence is preserved by contexts. -/
theorem CtxEquiv.plug {Δ : Ctx S.Base} {σ : Ty S.Base} {t t' : Term S Δ σ} (h : O.CtxEquiv t t')
    (C : Context S Δ σ Γ τ) : O.CtxEquiv (C.plug t) (C.plug t') := sorry

/-- The contextual preorder is preserved by application to an argument. -/
theorem CtxLe.app {σ : Ty S.Base} {t t' : Term S Γ (σ ⤳ τ)} (h : O.CtxLe t t')
    (u : Term S Γ σ) : O.CtxLe (.app t u) (.app t' u) := sorry

/-- At a ground type, every result of `t` is a result of `t'` when
`t ≤ctx t'` (use the trivial context). The one-sided form of
`CtxEquiv.eval_iff`. -/
theorem CtxLe.eval {γ : Ty S.Base} (hγ : O.Ground γ) {t t' : Term S [] γ}
    (h : O.CtxLe t t') {v : Term S [] γ} (hv : O.Eval t v) : O.Eval t' v := sorry

section Reflection

variable {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op')
  {O' : Observation (S.withOps Op')}

end Reflection

instance : Cslib.Congruence (O.CtxEquiv (Γ := Γ) (τ := τ)) := ⟨⟩

/-- Contextual equivalence is a CSLib congruence: an equivalence relation
preserved by every context. -/
instance : Cslib.LawfulCongruence (O.CtxEquiv (Γ := Γ) (τ := τ)) where
  refl := CtxEquiv.refl
  symm _ _ := CtxEquiv.symm
  trans _ _ _ := CtxEquiv.trans
  covariant := ⟨fun C _ _ h => h.plug C⟩

variable (O) in
/-- Contextual equivalence at `Γ ⊢ - : τ`, as a setoid. -/
def ctxSetoid (Γ : Ctx S.Base) (τ : Ty S.Base) : Setoid (Term S Γ τ) :=
  ⟨O.CtxEquiv, ⟨CtxEquiv.refl, CtxEquiv.symm, CtxEquiv.trans⟩⟩

variable (O) in
/-- The terms `Γ ⊢ t : τ` up to contextual equivalence. -/
abbrev CtxQuot (Γ : Ctx S.Base) (τ : Ty S.Base) : Type := Quotient (O.ctxSetoid Γ τ)

end Observation

end Silmaril.Calculi.TypedLambda
