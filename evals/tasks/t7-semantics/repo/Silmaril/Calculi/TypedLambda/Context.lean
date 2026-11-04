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
    (C.comp C').plug t = C.plug (C'.plug t) := by
  induction C with
  | hole => rfl
  | lam C ih => exact congrArg Term.lam ih
  | appL C u ih => exact congrArg (Term.app · u) ih
  | appR f C ih => exact congrArg (Term.app f) ih
  | op o args x C ih => exact congrArg (fun t => Term.op o (args.set x t)) ih

/-- Translate the operators of a context. -/
def mapOp {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op') :
    {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ → Context (S.withOps Op') Δ σ Γ τ
  | _, _, hole => hole
  | _, _, lam C => lam (C.mapOp f)
  | _, _, appL C u => appL (C.mapOp f) (u.mapOp f)
  | _, _, appR g C => appR (g.mapOp f) (C.mapOp f)
  | _, _, op o args x C => op (f o) (fun _ y => (args _ y).mapOp f) x (C.mapOp f)

theorem mapOp_set {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op')
    {as : List (Ty S.Base)} {α : Ty S.Base} (args : Tuple (Term S Γ) as) (x : Var as α)
    (t : Term S Γ α) :
    (fun _ y => ((args.set x t) _ y).mapOp f : Tuple (Term (S.withOps Op') Γ) as) =
      Tuple.set (fun _ y => (args _ y).mapOp f) x (t.mapOp f) := by
  induction x with
  | zero => exact Tuple.ext fun _ y => by cases y <;> rfl
  | succ x ih =>
      refine Tuple.ext fun _ y => ?_
      cases y with
      | zero => rfl
      | succ y => exact congrFun (congrFun (ih (Tuple.tail args) t) _) y

/-- Translating operators commutes with plugging. -/
theorem mapOp_plug {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op')
    (C : Context S Δ σ Γ τ) (t : Term S Δ σ) :
    (C.plug t).mapOp f = (C.mapOp f).plug (t.mapOp f) := by
  induction C with
  | hole => rfl
  | lam C ih => exact congrArg Term.lam ih
  | appL C u ih => exact congrArg (Term.app · _) ih
  | appR g C ih => exact congrArg (Term.app _) ih
  | op o args x C ih =>
      simp only [plug, Term.mapOp, mapOp]
      rw [← ih]
      exact congrArg _ (mapOp_set f args x _)

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
    Γ.length ≤ Δ.length
  | _, _, hole => le_rfl
  | _, _, lam C => Nat.le_of_succ_le C.length_le
  | _, _, appL C _ => C.length_le
  | _, _, appR _ C => C.length_le
  | _, _, op _ _ _ C => C.length_le

/-- A context whose own context is `α :: Γ` does not have its hole in `Γ`: the
hole context extends the context of the whole. (Used to rule out a `lam` above
the hole of a context whose hole is in the context of the whole.) -/
theorem ne_of_cons {α : Ty S.Base} (C : Context S Δ σ (α :: Γ) τ) : Δ ≠ Γ := by
  rintro rfl
  exact absurd C.length_le (by simp)

/-- Apply a substitution to every subterm of a context whose hole is in the
context of the whole (`h : Δ = Γ`). -/
def subst {Θ : Ctx S.Base} : {Γ : Ctx S.Base} → {τ : Ty S.Base} → Context S Δ σ Γ τ →
    Δ = Γ → Subst S Γ Θ → Context S Θ σ Θ τ
  | _, _, hole, _, _ => hole
  | _, _, lam C, h, _ => absurd h C.ne_of_cons
  | _, _, appL C u, h, s => appL (C.subst h s) (u.subst s)
  | _, _, appR f C, h, s => appR (f.subst s) (C.subst h s)
  | _, _, op o args x C, h, s => op o (fun _ y => (args _ y).subst s) x (C.subst h s)

/-- Substitution commutes with plugging. -/
theorem subst_plug {Θ : Ctx S.Base} (C : Context S Δ σ Γ τ) (h : Δ = Γ) (s : Subst S Γ Θ)
    (t : Term S Δ σ) : (C.plug t).subst s = (C.subst h s).plug (t.subst (h ▸ s)) := by
  induction C with
  | hole => rfl
  | lam C => exact absurd h C.ne_of_cons
  | appL C u ih => exact congrArg (Term.app · _) (ih h s)
  | appR f C ih => exact congrArg (Term.app _) (ih h s)
  | op o args x C ih =>
      simp only [plug, Term.subst, subst]
      rw [← ih h s]
      exact congrArg _ (Tuple.map_set (fun _ u => Term.subst s u) args x _)

/-- Substitution commutes with plugging, for a context whose hole and whole
share the context `Γ`. -/
theorem subst_plug_self {Θ : Ctx S.Base} (C : Context S Γ σ Γ τ) (s : Subst S Γ Θ)
    (t : Term S Γ σ) : (C.plug t).subst s = (C.subst rfl s).plug (t.subst s) :=
  subst_plug C rfl s t

/-- Substitution after substitution. -/
theorem subst_subst {Θ Θ' : Ctx S.Base} (C : Context S Δ σ Γ τ) (h : Δ = Γ)
    (s : Subst S Γ Θ) (s' : Subst S Θ Θ') :
    (C.subst h s).subst rfl s' = C.subst h (Subst.comp s' s) := by
  induction C with
  | hole => rfl
  | lam C => exact absurd h C.ne_of_cons
  | appL C u ih => simp only [subst, ih, Term.subst_subst]
  | appR f C ih => simp only [subst, ih, Term.subst_subst]
  | op o args x C ih => simp only [subst, ih, Term.subst_subst]

/-- A closed context is unchanged by substitution. -/
theorem subst_nil (C : Context S [] σ [] τ) (s : Subst S [] []) : C.subst rfl s = C := by
  suffices ∀ {Γ τ} (C : Context S [] σ Γ τ) (h : [] = Γ) (s : Subst S Γ []),
      C.subst h s = h ▸ C from this C rfl s
  intro Γ τ C h s
  induction C with
  | hole => rfl
  | lam C => exact absurd h C.ne_of_cons
  | appL C u ih => subst h; simp only [subst, ih rfl, Term.subst_nil]
  | appR f C ih => subst h; simp only [subst, ih rfl, Term.subst_nil]
  | op o args x C ih => subst h; simp only [subst, ih rfl, Term.subst_nil]

/-- Weaken a closed context into an arbitrary context `Γ`. -/
def ofClosed (C : Context S [] σ [] τ) : Context S Γ σ Γ τ := C.subst rfl fun _ x => x.elim

/-- The closed function `fun x:σ. C[x]` of a closed context. -/
def close (C : Context S [] σ [] τ) : Term S [] (σ ⤳ τ) :=
  .lam ((ofClosed (Γ := [σ]) C).plug (.var .zero))

/-- The body of `fun x:σ. C[x]`, instantiated at a closed term `t`, is `C[t]`. -/
theorem ofClosed_plug_subst1 (C : Context S [] σ [] τ) (t : Term S [] σ) :
    ((ofClosed (Γ := [σ]) C).plug (.var .zero)).subst1 t = C.plug t := by
  rw [Term.subst1, ofClosed, subst_plug_self, subst_subst, subst_nil]
  rfl

/-- CSLib's notation `C<[t]` for plugging. -/
instance : Cslib.HasHContext (Term S Γ τ) (Term S Δ σ) where
  Context := Context S Δ σ Γ τ
  fill := plug

theorem fill_eq_plug (C : Context S Δ σ Γ τ) (t : Term S Δ σ) :
    Cslib.HasHContext.fill (α := Term S Γ τ) C t = C.plug t := rfl

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

theorem ctxEquiv_iff_le {t t' : Term S Γ τ} : O.CtxEquiv t t' ↔ O.CtxLe t t' ∧ O.CtxLe t' t :=
  ⟨fun h => ⟨fun γ hγ C v => (h γ hγ C v).1, fun γ hγ C v => (h γ hγ C v).2⟩,
    fun h γ hγ C v => ⟨h.1 γ hγ C v, h.2 γ hγ C v⟩⟩

theorem CtxLe.refl (t : Term S Γ τ) : O.CtxLe t t := fun _ _ _ _ h => h

theorem CtxLe.trans {t₁ t₂ t₃ : Term S Γ τ} (h₁ : O.CtxLe t₁ t₂) (h₂ : O.CtxLe t₂ t₃) :
    O.CtxLe t₁ t₃ := fun γ hγ C v h => h₂ γ hγ C v (h₁ γ hγ C v h)

/-- The contextual preorder is preserved by contexts. -/
theorem CtxLe.plug {Δ : Ctx S.Base} {σ : Ty S.Base} {t t' : Term S Δ σ} (h : O.CtxLe t t')
    (C : Context S Δ σ Γ τ) : O.CtxLe (C.plug t) (C.plug t') := fun γ hγ C' v hv => by
  rw [← Context.plug_comp] at hv ⊢
  exact h γ hγ _ v hv

theorem CtxEquiv.refl (t : Term S Γ τ) : O.CtxEquiv t t := fun _ _ _ _ => Iff.rfl

theorem CtxEquiv.symm {t t' : Term S Γ τ} (h : O.CtxEquiv t t') : O.CtxEquiv t' t :=
  fun γ hγ C v => (h γ hγ C v).symm

theorem CtxEquiv.trans {t₁ t₂ t₃ : Term S Γ τ} (h₁ : O.CtxEquiv t₁ t₂) (h₂ : O.CtxEquiv t₂ t₃) :
    O.CtxEquiv t₁ t₃ := fun γ hγ C v => (h₁ γ hγ C v).trans (h₂ γ hγ C v)

/-- Contextual equivalence is preserved by contexts. -/
theorem CtxEquiv.plug {Δ : Ctx S.Base} {σ : Ty S.Base} {t t' : Term S Δ σ} (h : O.CtxEquiv t t')
    (C : Context S Δ σ Γ τ) : O.CtxEquiv (C.plug t) (C.plug t') :=
  ctxEquiv_iff_le.2 ⟨(ctxEquiv_iff_le.1 h).1.plug C, (ctxEquiv_iff_le.1 h).2.plug C⟩

/-- Remark 8: contextually equivalent complete programs have the same
results. -/
theorem CtxEquiv.eval_iff {γ : Ty S.Base} (hγ : O.Ground γ) {t t' : Term S [] γ}
    (h : O.CtxEquiv t t') (v : Term S [] γ) : O.Eval t v ↔ O.Eval t' v :=
  h γ hγ .hole v

/-- The contextual preorder is preserved by application to an argument. -/
theorem CtxLe.app {σ : Ty S.Base} {t t' : Term S Γ (σ ⤳ τ)} (h : O.CtxLe t t')
    (u : Term S Γ σ) : O.CtxLe (.app t u) (.app t' u) :=
  h.plug (.appL .hole u)

/-- The contextual preorder is preserved by application of a function. -/
theorem CtxLe.app_right {σ : Ty S.Base} (f : Term S Γ (σ ⤳ τ)) {t t' : Term S Γ σ}
    (h : O.CtxLe t t') : O.CtxLe (.app f t) (.app f t') :=
  h.plug (.appR f .hole)

/-- At a ground type, every result of `t` is a result of `t'` when
`t ≤ctx t'` (use the trivial context). The one-sided form of
`CtxEquiv.eval_iff`. -/
theorem CtxLe.eval {γ : Ty S.Base} (hγ : O.Ground γ) {t t' : Term S [] γ}
    (h : O.CtxLe t t') {v : Term S [] γ} (hv : O.Eval t v) : O.Eval t' v :=
  h γ hγ .hole v hv

section Reflection

variable {Op' : List (Ty S.Base) → Ty S.Base → Type} (f : OpMap S Op')
  {O' : Observation (S.withOps Op')}

/-- A translation of operators that preserves ground types and both preserves
and reflects the evaluation of complete programs reflects the contextual
preorder: the target language has at least as many contexts. -/
theorem CtxLe.of_mapOp (hG : ∀ {γ}, O.Ground γ → O'.Ground γ)
    (hfwd : ∀ {γ} {t v : Term S [] γ}, O.Eval t v → O'.Eval (t.mapOp f) (v.mapOp f))
    (hrefl : ∀ {γ} {t v : Term S [] γ}, O'.Eval (t.mapOp f) (v.mapOp f) → O.Eval t v)
    {t t' : Term S Γ τ} (h : O'.CtxLe (t.mapOp f) (t'.mapOp f)) : O.CtxLe t t' := by
  intro γ hγ C v hv
  have h' := h γ (hG hγ) (C.mapOp f) (v.mapOp f)
  rw [← Context.mapOp_plug, ← Context.mapOp_plug] at h'
  exact hrefl (h' (hfwd hv))

/-- Under the hypotheses of `CtxLe.of_mapOp`, the translation reflects
contextual equivalence. -/
theorem CtxEquiv.of_mapOp (hG : ∀ {γ}, O.Ground γ → O'.Ground γ)
    (hfwd : ∀ {γ} {t v : Term S [] γ}, O.Eval t v → O'.Eval (t.mapOp f) (v.mapOp f))
    (hrefl : ∀ {γ} {t v : Term S [] γ}, O'.Eval (t.mapOp f) (v.mapOp f) → O.Eval t v)
    {t t' : Term S Γ τ} (h : O'.CtxEquiv (t.mapOp f) (t'.mapOp f)) : O.CtxEquiv t t' :=
  ctxEquiv_iff_le.2
    ⟨CtxLe.of_mapOp f hG hfwd hrefl (ctxEquiv_iff_le.1 h).1,
      CtxLe.of_mapOp f hG hfwd hrefl (ctxEquiv_iff_le.1 h).2⟩

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
