module

public import Silmaril.Compile.PassAPI.Contract
public import Mathlib.Algebra.Group.End
public import Mathlib.Algebra.Group.Opposite
public import Mathlib.Algebra.Group.Hom.Defs
public import Mathlib.Logic.Function.Iterate

@[expose] public section

/-! # Pass API: verified passes and their monoid

A `Pass C` is a program transformation `run` that preserves the invariant `C.Inv` and
is sound for the contract: `C.Inv x → C.Rel x (run x)`. Its only data is `run`; the
other fields are proofs, so **two passes are equal iff their run functions are**
(`Pass.ext`).

**The monoid.** `Pass.seq p q` runs `p` then `q`; `Pass.id` does nothing. With
`p * q := p.seq q` (diagrammatic order) and `1 := Pass.id`, `Pass C` is a `Monoid`
(`instMonoid`); every monoid law holds by `rfl` on `run`. Consequences:

* `pipeline ps = ps.prod` (`pipeline_eq_prod`): **any list of verified passes is a
  verified pass**, and phase ordering is a word in the monoid;
* `repeatN n p = p ^ n` (`repeatN_eq_pow`), and `(p ^ n).run = p.run^[n]`;
* `toEnd : Pass C →* (Function.End X)ᵐᵒᵖ` is an injective monoid hom
  (`toEnd_injective`): the pass monoid is (isomorphic to) a submonoid of the
  endomorphisms, acting on the right (the opposite because `Function.End`
  multiplies by `∘`, while pipelines read left to right);
* a contract inclusion `C ≤ D` gives a monoid hom `weaken : Pass C →* Pass D`
  (`weaken_refl`, `weaken_trans`): contracts index a functor from the inclusion
  preorder to monoids.

Combinators beyond the monoid: `ite`/`guard` (decidable condition), `repeatFix`
(iterate with fuel and a stopping test), `best` (cheapest result by a cost function,
`best_le`), `ofOption` (a partial transformation with identity fallback), `foldSteps`
(a program-dependent sequence of sound steps). Translation validation is provided by
`CheckedPass`.

The categorical view in `Silmaril.Compile.PassAPI.Category` describes translations
between IRs over one semantic domain. This module assumes the supplied contract;
it does not establish that a particular contract models a source language.
-/

namespace Silmaril.Compile.PassAPI

universe u

variable {X : Type u} {C : Contract X}

/-- **A verified pass** for contract `C`: preserves the invariant and is sound. -/
@[ext] structure Pass (C : Contract X) where
  /-- The transformation. -/
  run : X → X
  inv_run : ∀ {x}, C.Inv x → C.Inv (run x)
  sound : ∀ {x}, C.Inv x → C.Rel x (run x)

namespace Pass

/-- The identity pass. -/
protected def id : Pass C := ⟨fun x => x, fun h => h, fun {x} _ => C.refl x⟩

/-- Sequencing: run `p`, then `q`. Sound by transitivity of the contract. -/
def seq (p q : Pass C) : Pass C where
  run x := q.run (p.run x)
  inv_run h := q.inv_run (p.inv_run h)
  sound h := C.trans (p.sound h) (q.sound (p.inv_run h))

/-- Named left-to-right sequencing: `p.then q` runs `p` before `q`. This is an
alias of `seq`, with the same executable function and proof fields. -/
abbrev «then» (p q : Pass C) : Pass C := p.seq q

@[simp] theorem then_run (p q : Pass C) (x : X) :
    (p.then q).run x = q.run (p.run x) := rfl

@[simp] theorem id_run (x : X) : (Pass.id : Pass C).run x = x := rfl
@[simp] theorem seq_run (p q : Pass C) (x : X) : (p.seq q).run x = q.run (p.run x) := rfl

/-! ## The monoid -/

/-- **Passes form a monoid** under sequencing (`p * q` runs `p` first). -/
instance instMonoid : Monoid (Pass C) where
  mul := seq
  one := Pass.id
  mul_assoc _ _ _ := rfl
  one_mul _ := rfl
  mul_one _ := rfl

@[simp] theorem mul_run (p q : Pass C) (x : X) : (p * q).run x = q.run (p.run x) := rfl
@[simp] theorem one_run (x : X) : (1 : Pass C).run x = x := rfl
theorem mul_def (p q : Pass C) : p * q = p.seq q := rfl
theorem one_def : (1 : Pass C) = Pass.id := rfl

/-- The run function of a power is the iterate. -/
theorem pow_run (p : Pass C) : ∀ n : Nat, (p ^ n).run = p.run^[n]
  | 0 => rfl
  | n + 1 => by
    funext x
    rw [pow_succ, mul_run, pow_run p n, Function.iterate_succ_apply']

/-- **Passes embed in the endomorphisms** (acting on the right). -/
def toEnd : Pass C →* (Function.End X)ᵐᵒᵖ where
  toFun p := MulOpposite.op p.run
  map_one' := rfl
  map_mul' _ _ := rfl

@[simp] theorem toEnd_apply (p : Pass C) : toEnd p = MulOpposite.op p.run := rfl

/-- The embedding is injective: a pass *is* its run function. -/
theorem toEnd_injective : Function.Injective (toEnd : Pass C →* (Function.End X)ᵐᵒᵖ) :=
  fun _ _ h => Pass.ext (MulOpposite.op_injective h)

/-! ## Pipelines -/

/-- **Pipelines**: run the passes left to right. -/
def pipeline : List (Pass C) → Pass C
  | [] => Pass.id
  | p :: ps => p.seq (pipeline ps)

@[simp] theorem pipeline_nil : pipeline ([] : List (Pass C)) = 1 := rfl
@[simp] theorem pipeline_cons (p : Pass C) (ps : List (Pass C)) :
    pipeline (p :: ps) = p * pipeline ps := rfl

/-- **A pipeline is the monoid product** of its passes. -/
theorem pipeline_eq_prod : ∀ ps : List (Pass C), pipeline ps = ps.prod
  | [] => rfl
  | p :: ps => by rw [pipeline_cons, List.prod_cons, pipeline_eq_prod ps]

theorem pipeline_append : ∀ ps qs : List (Pass C),
    pipeline (ps ++ qs) = pipeline ps * pipeline qs
  | [], qs => by simp
  | p :: ps, qs => by
    rw [List.cons_append, pipeline_cons, pipeline_cons, pipeline_append ps qs, mul_assoc]

/-- **Pipeline soundness** (an instance of `Pass.sound`; stated for search). -/
theorem pipeline_sound (ps : List (Pass C)) {x : X} (hx : C.Inv x) :
    C.Rel x ((pipeline ps).run x) ∧ C.Inv ((pipeline ps).run x) :=
  ⟨(pipeline ps).sound hx, (pipeline ps).inv_run hx⟩

/-- The run function of a pipeline is the composite of the run functions. -/
theorem pipeline_run : ∀ (ps : List (Pass C)) (x : X),
    (pipeline ps).run x = ps.foldl (fun y p => p.run y) x
  | [], _ => rfl
  | p :: ps, x => by simp [pipeline_run ps]

/-! ## Repetition -/

/-- Run `p` exactly `n` times. -/
def repeatN (n : Nat) (p : Pass C) : Pass C := pipeline (List.replicate n p)

theorem repeatN_eq_pow (n : Nat) (p : Pass C) : repeatN n p = p ^ n := by
  induction n with
  | zero => rfl
  | succ n ih =>
    rw [repeatN, List.replicate_succ, pipeline_cons, ← repeatN, ih, ← pow_succ']

/-- Iterate `p` at most `fuel` times, stopping as soon as `stop before after` holds (for
example: no change by a decidable equality, or no decrease of a size). Any `stop` is
sound; it only affects how much work is done. -/
def repeatFixRun (p : Pass C) (stop : X → X → Bool) : Nat → X → X
  | 0, x => x
  | n + 1, x => if stop x (p.run x) then p.run x else repeatFixRun p stop n (p.run x)

theorem repeatFixRun_spec (p : Pass C) (stop : X → X → Bool) :
    ∀ (n : Nat) {x : X}, C.Inv x →
      C.Inv (repeatFixRun p stop n x) ∧ C.Rel x (repeatFixRun p stop n x)
  | 0, x, hx => ⟨hx, C.refl x⟩
  | n + 1, x, hx => by
    unfold repeatFixRun
    split
    · exact ⟨p.inv_run hx, p.sound hx⟩
    · obtain ⟨h₁, h₂⟩ := repeatFixRun_spec p stop n (p.inv_run hx)
      exact ⟨h₁, C.trans (p.sound hx) h₂⟩

/-- **Iterate to a fixpoint, with fuel.** -/
def repeatFix (fuel : Nat) (stop : X → X → Bool) (p : Pass C) : Pass C where
  run := repeatFixRun p stop fuel
  inv_run h := (repeatFixRun_spec p stop fuel h).1
  sound h := (repeatFixRun_spec p stop fuel h).2

/-- `repeatFix` runs `p` some number of times, at most the fuel. -/
theorem repeatFix_run_eq_iterate (fuel : Nat) (stop : X → X → Bool) (p : Pass C) (x : X) :
    ∃ k ≤ fuel, (repeatFix fuel stop p).run x = p.run^[k] x := by
  induction fuel generalizing x with
  | zero => exact ⟨0, le_refl _, rfl⟩
  | succ n ih =>
    show ∃ k ≤ n + 1, repeatFixRun p stop (n + 1) x = p.run^[k] x
    unfold repeatFixRun
    split
    · exact ⟨1, by omega, rfl⟩
    · obtain ⟨k, hk, e⟩ := ih (p.run x)
      exact ⟨k + 1, by omega, by rw [Function.iterate_succ_apply]; exact e⟩

/-! ## Conditionals -/

/-- Run `p` where the decidable condition `c` holds, `q` elsewhere. -/
def ite (c : X → Prop) [DecidablePred c] (p q : Pass C) : Pass C where
  run x := if c x then p.run x else q.run x
  inv_run {x} h := by
    by_cases hc : c x
    · simp only [hc, ite_true]; exact p.inv_run h
    · simp only [hc, ite_false]; exact q.inv_run h
  sound {x} h := by
    by_cases hc : c x
    · simp only [hc, ite_true]; exact p.sound h
    · simp only [hc, ite_false]; exact q.sound h

/-- **Guard**: run `p` only where `c` holds. -/
def guard (c : X → Prop) [DecidablePred c] (p : Pass C) : Pass C := ite c p 1

@[simp] theorem guard_run (c : X → Prop) [DecidablePred c] (p : Pass C) (x : X) :
    (guard c p).run x = if c x then p.run x else x := rfl

/-- Guarding by a condition that always holds is the pass itself. -/
theorem guard_true (p : Pass C) : guard (fun _ => True) p = p := Pass.ext rfl

/-! ## Partial transformations -/

/-- A partial transformation (`none`: not applicable) whose successful results are sound
becomes a pass with identity fallback. -/
def ofOption (t : X → Option X)
    (hinv : ∀ {x y}, C.Inv x → t x = some y → C.Inv y)
    (hsound : ∀ {x y}, C.Inv x → t x = some y → C.Rel x y) : Pass C where
  run x := (t x).getD x
  inv_run {x} h := by
    cases e : t x with
    | none => exact h
    | some y => exact hinv h e
  sound {x} h := by
    cases e : t x with
    | none => exact C.refl x
    | some y => exact hsound h e

/-! ## Program-dependent step sequences -/

/-- Fold sound steps along a program-dependent index list (for example the defined
functions of a module). -/
def foldStepsRun {ι : Type} (idx : X → List ι) (step : X → ι → X) (x : X) : X :=
  (idx x).foldl step x

theorem foldl_steps_spec {ι : Type} (step : X → ι → X)
    (h : ∀ x i, C.Inv x → C.Inv (step x i) ∧ C.Rel x (step x i)) :
    ∀ (l : List ι) {x : X}, C.Inv x → C.Inv (l.foldl step x) ∧ C.Rel x (l.foldl step x)
  | [], x, hx => ⟨hx, C.refl x⟩
  | i :: l, x, hx => by
    obtain ⟨h₁, h₂⟩ := h x i hx
    obtain ⟨h₃, h₄⟩ := foldl_steps_spec step h l h₁
    exact ⟨h₃, C.trans h₂ h₄⟩

/-- **A program-dependent sequence of sound steps is a pass.** -/
def foldSteps {ι : Type} (idx : X → List ι) (step : X → ι → X)
    (h : ∀ x i, C.Inv x → C.Inv (step x i) ∧ C.Rel x (step x i)) : Pass C where
  run := foldStepsRun idx step
  inv_run hx := (foldl_steps_spec step h _ hx).1
  sound hx := (foldl_steps_spec step h _ hx).2

/-! ## Best of several -/

/-- Keep the cheapest of `x` and the candidates `p.run x` (ties keep the earlier). -/
def bestRun (cost : X → Nat) (ps : List (Pass C)) (x : X) : X :=
  ps.foldl (fun y p => if cost (p.run x) < cost y then p.run x else y) x

theorem bestRun_aux (cost : X → Nat) (x : X) (P : X → Prop) (hP : ∀ p : Pass C, P (p.run x)) :
    ∀ (ps : List (Pass C)) (y : X), P y →
      P (ps.foldl (fun y p => if cost (p.run x) < cost y then p.run x else y) y)
  | [], _, hy => hy
  | p :: ps, y, hy => by
    simp only [List.foldl_cons]
    apply bestRun_aux cost x P hP ps
    split
    · exact hP p
    · exact hy

/-- **Best of**: run every candidate and keep the cheapest result (or the input). Sound
because each candidate is. -/
def best (cost : X → Nat) (ps : List (Pass C)) : Pass C where
  run := bestRun cost ps
  inv_run {x} h := bestRun_aux cost x C.Inv (fun p => p.inv_run h) ps x h
  sound {x} h := bestRun_aux cost x (C.Rel x) (fun p => p.sound h) ps x (C.refl x)

theorem bestRun_le_aux (cost : X → Nat) (x : X) :
    ∀ (ps : List (Pass C)) (y : X),
      cost (ps.foldl (fun y p => if cost (p.run x) < cost y then p.run x else y) y) ≤ cost y ∧
      ∀ p ∈ ps,
        cost (ps.foldl (fun y p => if cost (p.run x) < cost y then p.run x else y) y) ≤
          cost (p.run x)
  | [], y => ⟨le_refl _, fun _ h => absurd h List.not_mem_nil⟩
  | p :: ps, y => by
    simp only [List.foldl_cons]
    set y' := if cost (p.run x) < cost y then p.run x else y with hy'
    have hy'y : cost y' ≤ cost y := by rw [hy']; split <;> omega
    have hy'p : cost y' ≤ cost (p.run x) := by rw [hy']; split <;> omega
    obtain ⟨h₁, h₂⟩ := bestRun_le_aux cost x ps y'
    refine ⟨le_trans h₁ hy'y, fun q hq => ?_⟩
    rcases List.mem_cons.mp hq with rfl | hq
    · exact le_trans h₁ hy'p
    · exact h₂ q hq

/-- `best` is no more expensive than its input or any candidate. -/
theorem best_le (cost : X → Nat) (ps : List (Pass C)) (x : X) :
    cost ((best cost ps).run x) ≤ cost x ∧ ∀ p ∈ ps, cost ((best cost ps).run x) ≤ cost (p.run x) :=
  bestRun_le_aux cost x ps x

/-! ## Weakening along contract inclusions -/

/-- A pass for `C` is a pass for any weaker contract `D ≥ C`. -/
def weakenFn {D : Contract X} (h : C ≤ D) (p : Pass C) : Pass D where
  run := p.run
  inv_run hx := (Contract.Le.inv_iff h _).mp (p.inv_run ((Contract.Le.inv_iff h _).mpr hx))
  sound hx :=
    have hx' := (Contract.Le.inv_iff h _).mpr hx
    Contract.Le.rel h _ _ hx' (p.sound hx')

/-- **Contract inclusions induce monoid homs.** -/
def weaken {D : Contract X} (h : C ≤ D) : Pass C →* Pass D where
  toFun := weakenFn h
  map_one' := rfl
  map_mul' _ _ := rfl

@[simp] theorem weaken_run {D : Contract X} (h : C ≤ D) (p : Pass C) :
    (weaken h p).run = p.run := rfl

theorem weaken_refl : weaken (le_refl C) = MonoidHom.id (Pass C) := MonoidHom.ext fun _ => rfl

theorem weaken_trans {D E : Contract X} (h₁ : C ≤ D) (h₂ : D ≤ E) :
    weaken (le_trans h₁ h₂) = (weaken h₂).comp (weaken h₁) := MonoidHom.ext fun _ => rfl

/-- Weakening commutes with the embedding into endomorphisms. -/
theorem toEnd_weaken {D : Contract X} (h : C ≤ D) (p : Pass C) :
    toEnd (weaken h p) = toEnd p := rfl

theorem weaken_injective {D : Contract X} (h : C ≤ D) : Function.Injective (weaken h) :=
  fun p q e => by
    have e' : (weaken h p).run = (weaken h q).run := congrArg Pass.run e
    exact Pass.ext e'

end Pass

end Silmaril.Compile.PassAPI
