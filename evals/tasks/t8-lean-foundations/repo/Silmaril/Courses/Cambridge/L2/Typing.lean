module

public import Silmaril.Courses.Cambridge.L2.Reduction

/-! # Cambridge L2 typing, §§4.3 and 4.6

The environment pairs the L1 location assumptions with variable assumptions.
Free variables use a finite map, while bound variables use a list matching the
de Bruijn syntax. Pushing onto the list represents the notes' `Γ,x:T` without
depending on the spelling of `x`. The rules are collected on pp. 66–67.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

structure TypeEnv where
  locs : L1.TypeEnv
  free : Finmap (fun _ : Var => Ty)
  bound : List Ty

def TypeEnv.empty : TypeEnv := ⟨∅, ∅, []⟩

def TypeEnv.push (Γ : TypeEnv) (type : Ty) : TypeEnv :=
  { Γ with bound := type :: Γ.bound }

inductive HasType : TypeEnv → Expr → Ty → Prop where
  | int {Γ : TypeEnv} (n : Int) : HasType Γ (.int n) .int
  | bool {Γ : TypeEnv} (b : Bool) : HasType Γ (.bool b) .bool
  | add {Γ : TypeEnv} {left right : Expr}
      (hl : HasType Γ left .int) (hr : HasType Γ right .int) :
      HasType Γ (.binary left .add right) .int
  | ge {Γ : TypeEnv} {left right : Expr}
      (hl : HasType Γ left .int) (hr : HasType Γ right .int) :
      HasType Γ (.binary left .ge right) .bool
  | cond {Γ : TypeEnv} {test yes no : Expr} {T : Ty}
      (hc : HasType Γ test .bool) (hy : HasType Γ yes T) (hn : HasType Γ no T) :
      HasType Γ (.cond test yes no) T
  | assign {Γ : TypeEnv} {loc : Loc} {e : Expr}
      (present : loc ∈ Γ.locs) (he : HasType Γ e .int) :
      HasType Γ (.assign loc e) .unit
  | deref {Γ : TypeEnv} {loc : Loc}
      (present : loc ∈ Γ.locs) : HasType Γ (.deref loc) .int
  | skip {Γ : TypeEnv} : HasType Γ .skip .unit
  | seq {Γ : TypeEnv} {left right : Expr} {T : Ty}
      (hl : HasType Γ left .unit) (hr : HasType Γ right T) :
      HasType Γ (.seq left right) T
  | loop {Γ : TypeEnv} {test body : Expr}
      (ht : HasType Γ test .bool) (hb : HasType Γ body .unit) :
      HasType Γ (.loop test body) .unit
  | bvar {Γ : TypeEnv} {index : Nat} {T : Ty}
      (lookup : Γ.bound[index]? = some T) :
      HasType Γ (.bvar index) T
  | fvar {Γ : TypeEnv} {name : Var} {T : Ty}
      (lookup : Finmap.lookup name Γ.free = some T) :
      HasType Γ (.fvar name) T
  | lam {Γ : TypeEnv} {domain codomain : Ty} {body : Expr}
      (typing : HasType (Γ.push domain) body codomain) :
      HasType Γ (.lam domain body) (.arr domain codomain)
  | app {Γ : TypeEnv} {function argument : Expr} {domain codomain : Ty}
      (hf : HasType Γ function (.arr domain codomain))
      (ha : HasType Γ argument domain) :
      HasType Γ (.app function argument) codomain
  | letE {Γ : TypeEnv} {type result : Ty} {value body : Expr}
      (hv : HasType Γ value type)
      (hb : HasType (Γ.push type) body result) :
      HasType Γ (.letE type value body) result
  | letRecFun {Γ : TypeEnv} {domain codomain result : Ty} {functionBody body : Expr}
      (hf : HasType ((Γ.push (.arr domain codomain)).push domain)
        functionBody codomain)
      (hb : HasType (Γ.push (.arr domain codomain)) body result) :
      HasType Γ (.letRecFun domain codomain functionBody body) result

/-- Syntax-directed inference for the complete L2 grammar. -/
def infer (Γ : TypeEnv) : Expr → Option Ty
  | .int _ => some .int
  | .bool _ => some .bool
  | .binary left .add right =>
      if infer Γ left = some .int ∧ infer Γ right = some .int then some .int else none
  | .binary left .ge right =>
      if infer Γ left = some .int ∧ infer Γ right = some .int then some .bool else none
  | .cond test yes no =>
      match infer Γ test, infer Γ yes, infer Γ no with
      | some .bool, some yesType, some noType =>
          if yesType = noType then some yesType else none
      | _, _, _ => none
  | .assign loc e =>
      if loc ∈ Γ.locs ∧ infer Γ e = some .int then some .unit else none
  | .deref loc => if loc ∈ Γ.locs then some .int else none
  | .skip => some .unit
  | .seq left right =>
      if infer Γ left = some .unit then infer Γ right else none
  | .loop test body =>
      if infer Γ test = some .bool ∧ infer Γ body = some .unit then some .unit else none
  | .bvar index => Γ.bound[index]?
  | .fvar name => Finmap.lookup name Γ.free
  | .lam domain body => (infer (Γ.push domain) body).map (.arr domain)
  | .app function argument =>
      match infer Γ function, infer Γ argument with
      | some (.arr domain codomain), some argumentType =>
          if domain = argumentType then some codomain else none
      | _, _ => none
  | .letE type value body =>
      if infer Γ value = some type then infer (Γ.push type) body else none
  | .letRecFun domain codomain functionBody body =>
      if infer ((Γ.push (.arr domain codomain)).push domain) functionBody =
          some codomain then
        infer (Γ.push (.arr domain codomain)) body
      else none

theorem infer_complete {Γ : TypeEnv} {e : Expr} {T : Ty}
    (typing : HasType Γ e T) : infer Γ e = some T := sorry

theorem infer_sound {Γ : TypeEnv} : ∀ e T, infer Γ e = some T → HasType Γ e T := sorry

theorem infer_iff_hasType {Γ : TypeEnv} {e : Expr} {T : Ty} :
    infer Γ e = some T ↔ HasType Γ e T := sorry

theorem typeable_iff_infer_isSome {Γ : TypeEnv} {e : Expr} :
    (∃ T, HasType Γ e T) ↔ (infer Γ e).isSome = true := sorry

theorem HasType.unique {Γ : TypeEnv} {e : Expr} {T U : Ty}
    (first : HasType Γ e T) (second : HasType Γ e U) : T = U := sorry

instance (Γ : TypeEnv) (e : Expr) (T : Ty) : Decidable (HasType Γ e T) :=
  decidable_of_iff (infer Γ e = some T) infer_iff_hasType

end Silmaril.Courses.Cambridge.L2
