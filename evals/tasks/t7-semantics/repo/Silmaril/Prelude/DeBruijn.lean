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

@[simp] theorem lift_id : lift id = id := by funext i; cases i <;> rfl

@[simp] theorem liftN_id (n : Nat) : liftN n id = id := by
  induction n with
  | zero => rfl
  | succ n ih => simp [liftN, ih]

@[simp] theorem lift_comp (ρ σ : Nat → Nat) : lift (σ ∘ ρ) = lift σ ∘ lift ρ := by
  funext i
  cases i <;> rfl

@[simp] theorem liftN_comp (n : Nat) (ρ σ : Nat → Nat) :
    liftN n (σ ∘ ρ) = liftN n σ ∘ liftN n ρ := by
  induction n with
  | zero => rfl
  | succ n ih => simp only [liftN, ih, lift_comp]

theorem liftN_add (n m : Nat) (ρ : Nat → Nat) :
    liftN (n + m) ρ = liftN n (liftN m ρ) := by
  induction n with
  | zero => simp only [Nat.zero_add, liftN]
  | succ n ih => simp only [Nat.succ_add, liftN, ih]

theorem liftN_lift (n : Nat) (ρ : Nat → Nat) :
    liftN n (lift ρ) = lift (liftN n ρ) := by
  induction n with
  | zero => rfl
  | succ n ih => simp only [liftN, ih]

theorem liftN_lt (ρ : Nat → Nat) {n i : Nat} (h : i < n) : liftN n ρ i = i := by
  induction n generalizing i with
  | zero => exact False.elim (Nat.not_lt_zero _ h)
  | succ n ih =>
      cases i with
      | zero => rfl
      | succ i => exact congrArg Nat.succ (ih (Nat.lt_of_succ_lt_succ h))

theorem liftN_add_index (n : Nat) (ρ : Nat → Nat) (i : Nat) :
    liftN n ρ (n + i) = n + ρ i := by
  induction n with
  | zero => simp only [liftN, Nat.zero_add]
  | succ n ih => simpa only [Nat.succ_add, liftN, lift] using congrArg Nat.succ ih

theorem liftN_ge (ρ : Nat → Nat) {n i : Nat} (h : n ≤ i) :
    liftN n ρ i = n + ρ (i - n) := by
  calc
    liftN n ρ i = liftN n ρ (n + (i - n)) := congrArg (liftN n ρ) (Nat.add_sub_of_le h).symm
    _ = n + ρ (i - n) := liftN_add_index n ρ (i - n)

end Silmaril.DeBruijn
