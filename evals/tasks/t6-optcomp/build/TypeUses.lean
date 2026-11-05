/-
The builder's view of which constants each statement names: for every constant of the given modules, the
constants of those modules that its type (not its value) refers to. One JSON object on stdout,
`{name: [name, ...]}`. A theorem named in a kept statement cannot be deleted from the starting repo.

Usage: `lean --run TypeUses.lean <module> ...`, with `LEAN_PATH` as for `Audit.lean`. Not part of the checker.
-/
import Lean

open Lean

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let modules := args.toArray.map String.toName
  let env ← importModules (modules.map ({ module := · })) {} (loadExts := false)
  let own : NameSet := modules.foldl (·.insert ·) {}
  let inOwn (d : Name) : Bool :=
    match env.getModuleIdxFor? d with
    | some i => (env.header.moduleNames[i.toNat]?.map own.contains).getD false
    | none => false
  let mut out : Array (String × Json) := #[]
  for (name, ci) in env.constants.map₁.toList do
    unless inOwn name do continue
    let used := ci.type.getUsedConstants.filter inOwn
    unless used.isEmpty do
      out := out.push (toString name, toJson (used.map toString))
  IO.println (Json.mkObj out.toList).compress
  return 0
