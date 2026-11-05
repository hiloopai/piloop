module

public import Silmaril.Courses.Cambridge.L1.Syntax
public import Silmaril.Semantics.Operational.Environment
public import Mathlib.Data.Finmap

/-! # Finite partial stores for Cambridge L1

`Finmap` represents the finite partial functions from locations to integers on
p. 14. The shared `Semantics.Operational.Environment` is their partial-function view.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1

open Silmaril.Semantics

abbrev Store := Finmap (fun _ : Loc => Int)

namespace Store

def empty : Store := ∅

def lookup (s : Store) (loc : Loc) : Option Int := Finmap.lookup loc s

/-- L1 assignment updates only an already allocated location. -/
def update (s : Store) (loc : Loc) (value : Int) : Option Store :=
  if loc ∈ s then some (s.insert loc value) else none

/-- A convenient finite presentation for concrete examples. Later entries win. -/
def fromList (entries : List (Loc × Int)) : Store :=
  entries.foldl (fun s (loc, value) => s.insert loc value) empty

def toEnvironment (s : Store) : Operational.Environment Loc Int :=
  fun loc => lookup s loc

theorem supported (s : Store) :
    Operational.Environment.SupportedBy (toEnvironment s) s.keys.toList := sorry

@[simp] theorem lookup_empty (loc : Loc) : lookup empty loc = none := sorry

theorem update_eq_some (s : Store) (loc : Loc) (value : Int) (h : loc ∈ s) :
    update s loc value = some (s.insert loc value) := sorry

theorem update_eq_none (s : Store) (loc : Loc) (value : Int) (h : loc ∉ s) :
    update s loc value = none := sorry

theorem toEnvironment_insert (s : Store) (loc : Loc) (value : Int) :
    toEnvironment (s.insert loc value) =
      Operational.Environment.set (toEnvironment s) loc value := sorry

end Store

end Silmaril.Courses.Cambridge.L1
