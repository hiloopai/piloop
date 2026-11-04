module

public import Silmaril.Prelude.DeBruijn

/-!
# Typed de Bruijn renamings

Lookup evidence `At Γ i A` for a de Bruijn index in a list context, and typed
renamings `TypedRen Γ Δ ρ`: functions on indices that send every index of `Γ`
to an index of `Δ` with the same type. The raw λ-calculus consumes them directly
(`Lambda.Raw.HasType.rename`), and `Lambda.Ren.Tracks.typedRen` /
`Lambda.Ren.ofTypedRen` identify intrinsic renamings with typed renamings restricted to
in-scope indices. The SSA calculi keep their own constants `Isotope.SSA.At` and
`Isotope.SSA.Ren`, which coincide with these definitionally (`SSA.At.iff_deBruijnAt`,
`SSA.Ren.iff_typedRen`) and whose lemmas delegate to the ones here.
-/

@[expose] public section

universe u

namespace Silmaril.DeBruijn

variable {τ : Type u}

/-- Lookup evidence for a de Bruijn index: index `i` of `Γ` has type `A`. -/
abbrev At (Γ : List τ) (i : Nat) (A : τ) : Prop := Γ[i]? = some A

/-- A typed renaming from `Γ` into `Δ`: the index function `ρ` preserves the types of
the indices of `Γ`. -/
def TypedRen (Γ Δ : List τ) (ρ : Nat → Nat) : Prop :=
  ∀ ⦃i A⦄, At Γ i A → At Δ (ρ i) A

namespace TypedRen

/-- The identity is a typed renaming. -/
theorem id (Γ : List τ) : TypedRen Γ Γ id := by intro i A h; simpa using h

/-- Typed renamings compose. -/
theorem comp {Γ Δ Θ : List τ} {ρ σ : Nat → Nat} (hρ : TypedRen Γ Δ ρ)
    (hσ : TypedRen Δ Θ σ) : TypedRen Γ Θ (σ ∘ ρ) := by intro i A h; exact hσ (hρ h)

/-- Lifting a typed renaming under a binder of type `A`. -/
theorem lift {Γ Δ : List τ} {ρ : Nat → Nat} (h : TypedRen Γ Δ ρ) (A : τ) :
    TypedRen (A :: Γ) (A :: Δ) (DeBruijn.lift ρ) := by
  intro i B hi
  cases i with
  | zero => simpa [At, DeBruijn.lift] using hi
  | succ i => simpa [At, DeBruijn.lift] using h hi

/-- Weakening by one binder of type `A` is a typed renaming. -/
theorem wk (Γ : List τ) (A : τ) : TypedRen Γ (A :: Γ) Nat.succ := by
  intro i B h
  simpa [At] using h

/-- Lifting a typed renaming under a whole context `Ξ` of binders. -/
theorem append {Γ Δ : List τ} {ρ : Nat → Nat} (h : TypedRen Γ Δ ρ) (Ξ : List τ) :
    TypedRen (Ξ ++ Γ) (Ξ ++ Δ) (DeBruijn.liftN Ξ.length ρ) := by
  induction Ξ with
  | nil => exact h
  | cons A Ξ ih => exact ih.lift A

end TypedRen

end Silmaril.DeBruijn
