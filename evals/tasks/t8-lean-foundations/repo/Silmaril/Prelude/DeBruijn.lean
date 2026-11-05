module

/-!
# Lifting de Bruijn renamings

These operations preserve newly bound indices and rename the older ones. They
can be used independently for value variables and block labels.
-/

@[expose] public section

namespace Silmaril.DeBruijn

/-- Lift a renaming through one newly bound index. -/
def lift (ρ : Nat → Nat) : Nat → Nat
  | 0 => 0
  | n + 1 => ρ n + 1

/-- Lift a renaming through `n` newly bound indices. -/
def liftN : Nat → (Nat → Nat) → Nat → Nat
  | 0, ρ => ρ
  | n + 1, ρ => lift (liftN n ρ)

theorem liftN_add (n m : Nat) (ρ : Nat → Nat) :
    liftN (n + m) ρ = liftN n (liftN m ρ) := sorry

theorem liftN_lift (n : Nat) (ρ : Nat → Nat) :
    liftN n (lift ρ) = lift (liftN n ρ) := sorry

theorem liftN_lt (ρ : Nat → Nat) {n i : Nat} (h : i < n) : liftN n ρ i = i := sorry

theorem liftN_ge (ρ : Nat → Nat) {n i : Nat} (h : n ≤ i) :
    liftN n ρ i = n + ρ (i - n) := sorry

end Silmaril.DeBruijn
