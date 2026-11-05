module

public import Silmaril.Courses.Cambridge.L1.Metatheory.Determinacy

/-! # Cambridge L1 Exercise 13.5: inversion of the last rule

Each result below lists every possible last rule for one source-expression
form. The proofs invert the relevant inductive derivation directly and do not
need induction hypotheses. This is the usable form of Lemmas 10–11 on p. 38.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

/-! ## Reduction inversion -/

theorem Step.binary_inversion {left right : Expr} {op : Op} {s : Store}
    {after : Config} (step : Step ⟨.binary left op right, s⟩ after) :
    (∃ a b, left = .int a ∧ op = .add ∧ right = .int b ∧
      after = ⟨.int (a + b), s⟩) ∨
    (∃ a b, left = .int a ∧ op = .ge ∧ right = .int b ∧
      after = ⟨.bool (a ≥ b), s⟩) ∨
    (∃ left' s', after = ⟨.binary left' op right, s'⟩ ∧
      Step ⟨left, s⟩ ⟨left', s'⟩) ∨
    (∃ right' s', IsValue left ∧ after = ⟨.binary left op right', s'⟩ ∧
      Step ⟨right, s⟩ ⟨right', s'⟩) := sorry

theorem Step.deref_inversion {loc : Loc} {s : Store} {after : Config}
    (step : Step ⟨.deref loc, s⟩ after) :
    ∃ n, Store.lookup s loc = some n ∧ after = ⟨.int n, s⟩ := sorry

theorem Step.assign_inversion {loc : Loc} {e : Expr} {s : Store}
    {after : Config} (step : Step ⟨.assign loc e, s⟩ after) :
    (∃ n, e = .int n ∧ loc ∈ s ∧ after = ⟨.skip, s.insert loc n⟩) ∨
    (∃ e' s', after = ⟨.assign loc e', s'⟩ ∧ Step ⟨e, s⟩ ⟨e', s'⟩) := sorry

theorem Step.seq_inversion {left right : Expr} {s : Store} {after : Config}
    (step : Step ⟨.seq left right, s⟩ after) :
    (left = .skip ∧ after = ⟨right, s⟩) ∨
    (∃ left' s', after = ⟨.seq left' right, s'⟩ ∧
      Step ⟨left, s⟩ ⟨left', s'⟩) := sorry

theorem Step.cond_inversion {test yes no : Expr} {s : Store}
    {after : Config} (step : Step ⟨.cond test yes no, s⟩ after) :
    (test = .bool true ∧ after = ⟨yes, s⟩) ∨
    (test = .bool false ∧ after = ⟨no, s⟩) ∨
    (∃ test' s', after = ⟨.cond test' yes no, s'⟩ ∧
      Step ⟨test, s⟩ ⟨test', s'⟩) := sorry

theorem Step.loop_inversion {test body : Expr} {s : Store}
    {after : Config} (step : Step ⟨.loop test body, s⟩ after) :
    after = ⟨.cond test (.seq body (.loop test body)) .skip, s⟩ := sorry

/-! ## Typing inversion -/

theorem HasType.int_inversion {Γ : TypeEnv} {n : Int} {T : Ty}
    (typing : HasType Γ (.int n) T) : T = .int := sorry

theorem HasType.bool_inversion {Γ : TypeEnv} {b : Bool} {T : Ty}
    (typing : HasType Γ (.bool b) T) : T = .bool := sorry

theorem HasType.binary_inversion {Γ : TypeEnv} {left right : Expr}
    {op : Op} {T : Ty} (typing : HasType Γ (.binary left op right) T) :
    (op = .add ∧ T = .int ∧ HasType Γ left .int ∧ HasType Γ right .int) ∨
    (op = .ge ∧ T = .bool ∧ HasType Γ left .int ∧ HasType Γ right .int) := sorry

theorem HasType.cond_inversion {Γ : TypeEnv} {test yes no : Expr} {T : Ty}
    (typing : HasType Γ (.cond test yes no) T) :
    HasType Γ test .bool ∧ HasType Γ yes T ∧ HasType Γ no T := sorry

theorem HasType.assign_inversion {Γ : TypeEnv} {loc : Loc} {e : Expr}
    {T : Ty} (typing : HasType Γ (.assign loc e) T) :
    T = .unit ∧ loc ∈ Γ ∧ HasType Γ e .int := sorry

theorem HasType.deref_inversion {Γ : TypeEnv} {loc : Loc} {T : Ty}
    (typing : HasType Γ (.deref loc) T) : T = .int ∧ loc ∈ Γ := sorry

theorem HasType.skip_inversion {Γ : TypeEnv} {T : Ty}
    (typing : HasType Γ .skip T) : T = .unit := sorry

theorem HasType.seq_inversion {Γ : TypeEnv} {left right : Expr} {T : Ty}
    (typing : HasType Γ (.seq left right) T) :
    HasType Γ left .unit ∧ HasType Γ right T := sorry

theorem HasType.loop_inversion {Γ : TypeEnv} {test body : Expr} {T : Ty}
    (typing : HasType Γ (.loop test body) T) :
    T = .unit ∧ HasType Γ test .bool ∧ HasType Γ body .unit := sorry

end Silmaril.Courses.Cambridge.L1
