/-
The task's trusted checker. It reads only compiled `.olean` files, never the sources, and prints one
JSON object on stdout:

* `replay`: `"ok"`, or the kernel's error, after replaying every constant of the task's modules
  through the kernel on top of their external imports (Mathlib, Lean core). This is
  `leanchecker`'s check, done for all modules in one process.
* `constants`: for every constant of the task's modules, its kind, the structural hash of its type
  and, for a definition, opaque or instance, of its value. A theorem's value is its proof and is not
  hashed, so a changed proof is free and a changed statement or definition is visible.
  `uses` names the constants of the task's modules that it refers to, `line` where it is declared.
* `axioms`: for every theorem of the task's modules, the axioms its proof depends on, collected by
  walking the bodies of every constant it uses (never from the precomputed per-module tables a
  build writes, which a doctored olean could forge).

Usage: `lean --run Audit.lean <module> ...`, with `LEAN_PATH` naming the project's and its
packages' build directories.
-/
import Lean
import Lean.Replay

open Lean

def kindOf : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "def"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quot"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "ctor"
  | .recInfo _ => "rec"

/-- The hash of what a constant means: its type, its universe parameters and, for anything but a
theorem, its value or its constructors. -/
def meaningHash (ci : ConstantInfo) : UInt64 :=
  let base := mixHash (hash ci.levelParams) ci.type.hash
  match ci with
  | .defnInfo d => mixHash base d.value.hash
  | .opaqueInfo d => mixHash base d.value.hash
  | .inductInfo d => mixHash base (mixHash (hash d.ctors) (hash (d.numParams, d.numIndices, d.isUnsafe)))
  | .ctorInfo d => mixHash base (hash (d.induct, d.cidx, d.numFields, d.isUnsafe))
  | .axiomInfo d => mixHash base (hash d.isUnsafe)
  | _ => base

/-- The axioms each constant depends on, memoised across calls. -/
partial def axiomsOf (env : Environment) (memo : IO.Ref (Std.HashMap Name (Array Name)))
    (c : Name) : IO (Array Name) := do
  if let some r := (← memo.get)[c]? then return r
  memo.modify (·.insert c #[])  -- a cycle (mutual definitions) contributes nothing twice
  let some ci := env.find? c | return #[]
  let mut acc : NameSet := {}
  if let .axiomInfo _ := ci then acc := acc.insert c
  for d in ci.getUsedConstantsAsSet do
    for a in (← axiomsOf env memo d) do acc := acc.insert a
  let r := acc.toArray.qsort Name.lt
  memo.modify (·.insert c r)
  return r

unsafe def readParts (module : Name) : IO (Array (ModuleData × CompactedRegion)) := do
  let mFile ← findOLean module
  unless (← mFile.pathExists) do
    throw <| IO.userError s!"no olean for {module}: {mFile}"
  let mut names := #[mFile]
  let sFile := OLeanLevel.server.adjustFileName mFile
  if (← sFile.pathExists) then
    names := names.push sFile
    let pFile := OLeanLevel.private.adjustFileName mFile
    if (← pFile.pathExists) then names := names.push pFile
  readModuleDataParts names

/-- Replay every constant of `modules` through the kernel, on top of the imports that are not
themselves among `modules`. -/
unsafe def replay (modules : Array Name) : IO String := do
  let own : NameSet := modules.foldl (·.insert ·) {}
  let mut external : Array Import := #[]
  let mut seen : NameSet := {}
  let mut fresh : Std.HashMap Name ConstantInfo := {}
  for m in modules do
    let parts ← readParts m
    let some (head, _) := parts[0]? | throw <| IO.userError s!"empty olean for {m}"
    for i in head.imports do
      unless own.contains i.module || seen.contains i.module do
        seen := seen.insert i.module
        external := external.push i
    let some (last, _) := parts.back? | unreachable!
    for name in last.constNames, ci in last.constants do
      fresh := fresh.insert name ci
  let (_, s) ← importModulesCore external |>.run
  let env ← finalizeImport s external {} 0 false false (isModule := true)
  try
    discard <| env.toKernelEnv.replay fresh
    return "ok"
  catch e =>
    return s!"{e}"

unsafe def main (args : List String) : IO UInt32 := do
  initSearchPath (← findSysroot)
  let modules := args.toArray.map String.toName
  let replayed ← replay modules
  let env ← importModules (modules.map ({ module := · })) {} (loadExts := false)
  let own : NameSet := modules.foldl (·.insert ·) {}
  let memo ← IO.mkRef {}
  let mut consts : Array Json := #[]
  let mut axioms : Array Json := #[]
  for (name, ci) in env.constants.map₁.toList do
    let some idx := env.getModuleIdxFor? name | continue
    let some mod := env.header.moduleNames[idx.toNat]? | continue
    unless own.contains mod do continue
    let uses := ci.getUsedConstantsAsSet.toArray.filter fun d =>
      match env.getModuleIdxFor? d with
      | some i => (env.header.moduleNames[i.toNat]?.map own.contains).getD false
      | none => false
    consts := consts.push <| Json.mkObj [
      ("name", toJson name), ("module", toJson mod), ("kind", toJson (kindOf ci)),
      ("hash", toJson (toString (meaningHash ci))), ("uses", toJson (uses.map toString)),
      ("line", toJson ((declRangeExt.find? (level := .server) env name).map (·.range.pos.line)))]
    if let .thmInfo _ := ci then
      axioms := axioms.push <| Json.mkObj [
        ("name", toJson name), ("axioms", toJson ((← axiomsOf env memo name).map toString))]
  IO.println <| (Json.mkObj [("replay", toJson replayed), ("constants", Json.arr consts),
    ("axioms", Json.arr axioms)]).compress
  return 0
