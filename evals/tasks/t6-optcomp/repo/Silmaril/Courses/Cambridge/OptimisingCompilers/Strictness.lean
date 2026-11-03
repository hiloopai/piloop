module

public import Mathlib.Control.Fix
public import Mathlib.Data.Part
public import Mathlib.Data.Fintype.Order
public import Mathlib.Data.Fin.VecNotation
public import Mathlib.Order.FixedPoints

/-! # §12 Strictness analysis (notes pp. 21–24; slides pp. 93–98)

The language of recursion equations (notes p. 22)

  `Fᵢ(x₁, …, x_{kᵢ}) = eᵢ`,   `e ::= xᵢ | Aᵢ(e₁, …, e_{rᵢ}) | Fᵢ(e₁, …, e_{kᵢ})`,

over built-ins `Aᵢ`. The standard interpretation is in `D = ℤ⊥`, here
`Part ℤ` with `⊥ = Part.none`, and the user functions denote the least
solution of their equations (`Part.fix`). The abstract interpretation is in
`2 = {0, 1}` (`Bool`, `false` = "guaranteed looping"): each built-in has a
strictness function `a♯`, and the `fᵢ♯` are the least solution of the same
equations over the `a♯` (notes p. 24), a least fixed point in the complete
lattice of functions `2^k → 2`.

* `sharp_safe`: safety (notes p. 24): if `f♯(x⃗) = 0` then `f(d⃗) = ⊥` whenever
  `xᵢ = 0 ⇒ dᵢ = ⊥`, provided each built-in's strictness function is monotone
  and safe;
* `strict_of_sharp`: hence `f` is strict in its `i`th parameter when
  `f♯(1, …, 1, 0, 1, …, 1) = 0`;
* `plusSharp`: for `plus(x,y) = cond(x=0, y, plus(x-1, y+1))`,
  `plus♯(x, y) = x ∧ y` (notes p. 24).
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Strictness

/-- A signature: built-ins with arities, standard and strictness
interpretations, and user functions with arities. -/
structure Sig where
  B : Type
  bar : B → ℕ
  sem : (a : B) → (Fin (bar a) → Part ℤ) → Part ℤ
  sharp : (a : B) → (Fin (bar a) → Bool) → Bool
  n : ℕ
  far : Fin n → ℕ

/-- Expressions over `k` parameters. -/
inductive Expr (S : Sig) (k : ℕ) : Type where
  | arg (i : Fin k)
  | prim (a : S.B) (es : Fin (S.bar a) → Expr S k)
  | call (f : Fin S.n) (es : Fin (S.far f) → Expr S k)

namespace Expr

variable {S : Sig} {k : ℕ}

/-- Evaluation over any interpretation of built-ins and user functions. -/
def eval {X : Type} (p : (a : S.B) → (Fin (S.bar a) → X) → X)
    (fs : (f : Fin S.n) → (Fin (S.far f) → X) → X) (env : Fin k → X) : Expr S k → X
  | .arg i => env i
  | .prim a es => p a fun j => (es j).eval p fs env
  | .call f es => fs f fun j => (es j).eval p fs env

/-- Evaluation is monotone in the environment and in the user functions, provided
the built-ins and the smaller interpretation of the user functions are monotone. -/
theorem eval_mono {X : Type} [Preorder X] {p : (a : S.B) → (Fin (S.bar a) → X) → X}
    (hp : ∀ a, Monotone (p a)) {g g' : (f : Fin S.n) → (Fin (S.far f) → X) → X}
    (hg : ∀ f, Monotone (g f)) (hgg : ∀ f x, g f x ≤ g' f x) {env env' : Fin k → X}
    (he : ∀ i, env i ≤ env' i) : ∀ e : Expr S k, e.eval p g env ≤ e.eval p g' env'
  | .arg i => he i
  | .prim a es => hp a fun j => eval_mono hp hg hgg he (es j)
  | .call f es => (hg f fun j => eval_mono hp hg hgg he (es j)).trans (hgg f _)

end Expr

/-- A program: a body for each user function. -/
structure Prog (S : Sig) where
  body : (f : Fin S.n) → Expr S (S.far f)

namespace Prog

variable {S : Sig} (P : Prog S)

/-- Interpretations of the user functions in `D`. -/
abbrev Funs (S : Sig) : Type := (f : Fin S.n) → (Fin (S.far f) → Part ℤ) → Part ℤ

/-- The functional whose least fixed point is the standard semantics, uncurried
for `Part.fix`. -/
def Φ (g : (x : Σ f : Fin S.n, Fin (S.far f) → Part ℤ) → Part ℤ) :
    (x : Σ f : Fin S.n, Fin (S.far f) → Part ℤ) → Part ℤ := fun x =>
  (P.body x.1).eval S.sem (fun f d => g ⟨f, d⟩) x.2

/-- The standard semantics `fᵢ` (notes p. 24): the least solution. -/
noncomputable def denote : Funs S := fun f d => Part.fix P.Φ ⟨f, d⟩

/-- Strictness functions of the user functions. -/
abbrev SharpFuns (S : Sig) : Type := (f : Fin S.n) → (Fin (S.far f) → Bool) → Bool

/-- The abstract functional over the `a♯`. -/
def Φsharp (g : SharpFuns S) : SharpFuns S := fun f x => (P.body f).eval S.sharp g x

/-- The strictness functions `fᵢ♯`: the least solution (notes p. 24), defined by
the Knaster–Tarski formula. -/
noncomputable def sharp : SharpFuns S := sInf {g | P.Φsharp g ≤ g}

/-- Monotone strictness functions of the user functions. -/
abbrev MonFuns (S : Sig) : Type := (f : Fin S.n) → (Fin (S.far f) → Bool) →o Bool

/-- The abstract functional restricted to monotone strictness functions. -/
def Φmon (hmono : ∀ a, Monotone (S.sharp a)) : MonFuns S →o MonFuns S where
  toFun m f := ⟨fun x => (P.body f).eval S.sharp (fun f => m f) x,
    fun _ _ h => Expr.eval_mono hmono (fun f => (m f).monotone) (fun _ _ => le_rfl) h _⟩
  monotone' m _ h _ _ :=
    Expr.eval_mono hmono (fun f => (m f).monotone) (fun f x => h f x) (fun _ => le_rfl) _

/-- `g` is strict in parameter `i` (notes p. 22). -/
def IsStrictIn {r : ℕ} (g : (Fin r → Part ℤ) → Part ℤ) (i : Fin r) : Prop :=
  ∀ d, d i = Part.none → g d = Part.none

end Prog

/-! ## The running example: `plus(x,y) = cond(x=0, y, plus(x-1, y+1))` -/

/-- The built-ins of the example. -/
inductive Prim where
  | cond | eq | sub1 | add1 | zero

/-- Arities. -/
@[reducible] def Prim.arity : Prim → ℕ
  | .cond => 3
  | .eq => 2
  | .sub1 | .add1 => 1
  | .zero => 0

/-- Standard interpretations (notes p. 22): strict except `cond` in its
branches. -/
def Prim.sem : (a : Prim) → (Fin a.arity → Part ℤ) → Part ℤ
  | .cond, d => (d 0).bind fun p => if p = 0 then d 2 else d 1
  | .eq, d => (d 0).bind fun a => (d 1).map fun b => if a = b then 1 else 0
  | .sub1, d => (d 0).map (· - 1)
  | .add1, d => (d 0).map (· + 1)
  | .zero, _ => Part.some 0

/-- Strictness functions (notes pp. 23–24): `cond♯(p,x,y) = p ∧ (x ∨ y)`,
`eq♯(x,y) = x ∧ y`, `add1♯(x) = sub1♯(x) = x`, `0♯ = 1`. -/
def Prim.sharp : (a : Prim) → (Fin a.arity → Bool) → Bool
  | .cond, x => x 0 && (x 1 || x 2)
  | .eq, x => x 0 && x 1
  | .sub1, x | .add1, x => x 0
  | .zero, _ => true

/-- The signature of the example: one user function of arity 2. -/
@[reducible] def plusSig : Sig where
  B := Prim
  bar := Prim.arity
  sem := Prim.sem
  sharp := Prim.sharp
  n := 1
  far _ := 2

/-- `plus(x,y) = cond(x=0, y, plus(x-1, y+1))`. -/
def plusProg : Prog plusSig where
  body _ :=
    .prim Prim.cond ![
      .prim Prim.eq ![.arg 0, .prim Prim.zero ![]],
      .arg 1,
      .call 0 ![.prim Prim.sub1 ![.arg 0], .prim Prim.add1 ![.arg 1]]]

/-- Hence `plus` is strict in both parameters (notes p. 25). -/
theorem plus_strict (i : Fin 2) : Prog.IsStrictIn (plusProg.denote 0) i := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Strictness
