module

public import Silmaril.Calculi.TypedLambda.Substitution

/-! # PCF and PCF+`por`

Figure 1 of the denotational semantics notes (§5.1), with the extension of
Figure 5 (§8.2). Both languages are one signature, indexed by a flag
`p : Bool`. The parallel-or operator exists only when `p = true`, so
`Term false` is PCF, `Term true` is PCF+`por`, and every definition and proof
stated for an arbitrary `p` covers both.

The constructs of the notes are fully applied operators:

| Notes | Operator | Arguments | Result |
| --- | --- | --- | --- |
| `𝟶` | `zero` | | `nat` |
| `succ(t)`, `pred(t)` | `succ`, `pred` | `nat` | `nat` |
| `true`, `false` | `tt`, `ff` | | `bool` |
| `zero?(t)` | `isZero` | `nat` | `bool` |
| `if b then t else t'` | `ite τ` | `bool`, `τ`, `τ` | `τ` |
| `fix(t)` | `fix τ` | `τ -> τ` | `τ` |
| `por(t, t')` | `por` | `bool`, `bool` | `bool` |

Variables, `fun x:σ. t` and application come from `Silmaril.Calculi.TypedLambda.Term`.
Figure 5 of the notes types `por(t₁, t₂)` at an arbitrary `τ`; the premises
and the evaluation rules show that `bool` is intended.
-/

@[expose] public section

namespace Silmaril.Calculi.PCF


open TypedLambda Cslib.LambdaCalculus.LocallyNameless.Stlc

/-- The ground types of PCF. -/
inductive Base
  /-- Natural numbers. -/
  | nat
  /-- Booleans. -/
  | bool
  deriving DecidableEq

/-- PCF types, `τ ::= nat ∣ bool ∣ τ -> τ`. -/
abbrev Ty := TypedLambda.Ty Base

/-- The type `nat`. -/
abbrev Ty.nat : Ty := .base .nat

/-- The type `bool`. -/
abbrev Ty.bool : Ty := .base .bool

/-- The ground types, at which programs are observed (§5.3). -/
def Ty.IsGround : Ty → Prop
  | .base _ => True
  | .arrow _ _ => False

/-- The operators of PCF, and of PCF+`por` when the flag is `true`. -/
inductive Op : Bool → List Ty → Ty → Type
  /-- `𝟶`. -/
  | zero {p : Bool} : Op p [] .nat
  /-- `succ(t)`. -/
  | succ {p : Bool} : Op p [.nat] .nat
  /-- `pred(t)`. -/
  | pred {p : Bool} : Op p [.nat] .nat
  /-- `true`. -/
  | tt {p : Bool} : Op p [] .bool
  /-- `false`. -/
  | ff {p : Bool} : Op p [] .bool
  /-- `zero?(t)`. -/
  | isZero {p : Bool} : Op p [.nat] .bool
  /-- `if b then t else t'`. -/
  | ite {p : Bool} (τ : Ty) : Op p [.bool, τ, τ] τ
  /-- `fix(t)`. -/
  | fix {p : Bool} (τ : Ty) : Op p [τ ⤳ τ] τ
  /-- Parallel or, Figure 5. -/
  | por : Op true [.bool, .bool] .bool

/-- The signature of PCF (`p = false`) or PCF+`por` (`p = true`). -/
abbrev sig (p : Bool) : Signature := ⟨Base, Op p⟩

/-- PCF terms, `Pcf_{Γ,τ}`, or PCF+`por` terms when `p = true`. -/
abbrev Term (p : Bool) (Γ : Ctx Base) (τ : Ty) : Type := TypedLambda.Term (sig p) Γ τ

/-- Closed terms, `Pcf_τ`. -/
abbrev Closed (p : Bool) (τ : Ty) : Type := Term p [] τ

namespace Term

variable {p : Bool} {Γ : Ctx Base} {σ τ : Ty}

/-- `𝟶`. -/
abbrev zero : Term p Γ .nat := .op .zero .nil

/-- `succ(t)`. -/
abbrev succ (t : Term p Γ .nat) : Term p Γ .nat := .op .succ (.single t)

/-- `pred(t)`. -/
abbrev pred (t : Term p Γ .nat) : Term p Γ .nat := .op .pred (.single t)

/-- `true`. -/
abbrev tt : Term p Γ .bool := .op .tt .nil

/-- `false`. -/
abbrev ff : Term p Γ .bool := .op .ff .nil

/-- `zero?(t)`. -/
abbrev isZero (t : Term p Γ .nat) : Term p Γ .bool := .op .isZero (.single t)

/-- `if b then t else t'`. -/
abbrev ite (b : Term p Γ .bool) (t t' : Term p Γ τ) : Term p Γ τ :=
  .op (.ite τ) (b ::ₜ t ::ₜ t' ::ₜ .nil)

/-- `fix(t)`. -/
abbrev fix (t : Term p Γ (τ ⤳ τ)) : Term p Γ τ := .op (.fix τ) (.single t)

/-- `por(t, t')`. -/
abbrev por (t t' : Term true Γ .bool) : Term true Γ .bool := .op .por (t ::ₜ t' ::ₜ .nil)

/-- The numeral `n̄`: `0̄ = 𝟶` and `n+1 = succ(n̄)`. -/
def num : ℕ → Term p Γ .nat
  | 0 => zero
  | n + 1 => succ (num n)

/-- The boolean literal for `b`. -/
def bool : Bool → Term p Γ .bool
  | true => tt
  | false => ff

/-- The variable bound by the innermost binder. -/
abbrev v0 : Term p (σ :: Γ) σ := .var .zero

/-- Example 18: the diverging term `Ωτ = fix(fun x:τ. x)`. -/
abbrev omega (τ : Ty) : Term p Γ τ := fix (.lam v0)

@[simp] theorem num_zero : (num 0 : Term p Γ .nat) = zero := sorry

@[simp] theorem num_succ (n : ℕ) : (num (n + 1) : Term p Γ .nat) = succ (num n) := sorry

section Subst

variable {Δ : Ctx Base} (s : Subst (sig p) Γ Δ) (ρ : Ren (S := sig p) Γ Δ)

@[simp] theorem rename_num (n : ℕ) : (num n : Term p Γ .nat).rename ρ = num n := sorry

end Subst

end Term

/-- The values, or canonical forms, of Figure 3:
`v ::= 𝟶 ∣ succ(v) ∣ true ∣ false ∣ fun x:τ. t`.

Rules about operators quantify over an arbitrary argument tuple (`args`) rather
than using the smart constructors, so that case analysis only has to unify a
variable with a tuple. `Value.succ' hv : Value (.succ v)` is the smart form. -/
inductive Value {p : Bool} : {τ : Ty} → Closed p τ → Prop
  /-- `𝟶`. -/
  | zero : Value .zero
  /-- `succ(v)`. -/
  | succ {args : Tuple (Closed p) [.nat]} : Value args.head → Value (.op .succ args)
  /-- `true`. -/
  | tt : Value .tt
  /-- `false`. -/
  | ff : Value .ff
  /-- `fun x:σ. t`. -/
  | lam {σ τ : Ty} (t : Term p [σ] τ) : Value (.lam t)

namespace Value

variable {p : Bool}

theorem bool (b : Bool) : Value (Term.bool (p := p) (Γ := []) b) := sorry

/-- The canonical forms at each type: numerals, boolean literals and
abstractions. -/
def Canonical : (τ : Ty) → Closed p τ → Prop
  | .base .nat, v => ∃ n, v = Term.num n
  | .base .bool, v => ∃ b, v = Term.bool b
  | .arrow _ _, v => ∃ t, v = .lam t

end Value

theorem Term.num_injective {p : Bool} {Γ : Ctx Base} :
    Function.Injective (Term.num (p := p) (Γ := Γ)) := sorry

/-! ### Testing for a ground value

`eqₙ` and `eq_b` test whether a term evaluates to a given numeral or boolean.
They are used for the application contexts of Lemma 37 and for the finite
elements of full abstraction. -/

section Tests

variable {p : Bool} {Γ : Ctx Base}

/-- `eqₙ(e)`, true iff `e` evaluates to the numeral `n̄`:
`eq₀(e) = zero?(e)` and `eqₙ₊₁(e) = if zero?(e) then false else eqₙ(pred(e))`. -/
def Term.eqNum : ℕ → Term p Γ .nat → Term p Γ .bool
  | 0, e => Term.isZero e
  | n + 1, e => Term.ite (Term.isZero e) Term.ff (eqNum n (Term.pred e))

/-- `eq_b(e)`, true iff `e` evaluates to `b`: `if e then true else false` or
`if e then false else true`. -/
def Term.eqBool : Bool → Term p Γ .bool → Term p Γ .bool
  | true, e => Term.ite e Term.tt Term.ff
  | false, e => Term.ite e Term.ff Term.tt

end Tests

end Silmaril.Calculi.PCF
