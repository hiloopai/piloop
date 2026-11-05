module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.ConstraintBased
public import Mathlib.Order.Setoid.Basic

/-! # §18 Points-to and alias analysis (notes pp. 32–35; slides pp. 113–116)

Pointer statements of notes p. 33:

  `x := newℓ`, `x := null`, `x := &y`, `x := y`, `x := *y`, `*x := y`.

*Andersen's analysis* (notes §18.1, p. 34): abstract values
`V = Var ∪ {newℓ} ∪ {null}`, a single flow-insensitive points-to function
`pt : V → P(V)`, and the constraints

  `⊢ x := &y : y ∈ pt(x)`,  `⊢ x := null : null ∈ pt(x)`,
  `⊢ x := newℓ : newℓ ∈ pt(x)`,  `⊢ x := y : pt(y) ⊆ pt(x)`,
  `z ∈ pt(y) ⇒ ⊢ x := *y : pt(z) ⊆ pt(x)`,  `z ∈ pt(x) ⇒ ⊢ *x := y : pt(y) ⊆ pt(z)`.

The solutions form a Moore family, so the least solution `andersen` exists.
Safety (notes p. 34): "at all program points during execution, the value of
pointer variable `x` is always in the description `pt(x)`"
(`andersen_sound`), for executions running the statements in any order (the
analysis is flow-insensitive). Hence the alias test: if
`pt(x) ∩ pt(y) = ∅` then `x` and `y` never point to the same location
(`noAlias_sound`).

*Steensgaard's analysis* (slides p. 116; Steensgaard 1996, Figure 3)
unifies instead of including. A `SteensgaardModel` is an equivalence relation
on abstract values (the type variables) with a points-to function that is
constant on classes and whose value is empty or a single whole class; the
statement constraints (`Stmt.SteensSat`) use Steensgaard's conditional join
`τ₂ ⊴ τ₁ ↔ τ₂ = ⊥ ∨ τ₂ = τ₁` (`CJoin`) in place of Andersen's inclusion, so
`x := y` merges the targets of `x` and `y`. Every model is an Andersen solution
(`SteensgaardModel.isAndersen`), hence safe (`SteensgaardModel.sound`) and above
`andersen` (`SteensgaardModel.andersen_le`); models always exist
(`SteensgaardModel.top`), and the inclusion can be strict
(`steensgaard_strict`). The skeleton's `SteensgaardSol` (which assumes the
Andersen constraints) is derived from a model by `SteensgaardModel.toSol`.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.PointsTo

open ConstraintBased

/-- Pointer statements over variables `V` and allocation sites `ℓ : ℕ`. -/
inductive Stmt (V : Type*) where
  | new (x : V) (ℓ : ℕ)
  | null (x : V)
  | addr (x y : V)
  | copy (x y : V)
  | load (x y : V)
  | store (x y : V)

/-- Abstract values `Var ∪ {newℓ} ∪ {null}` (notes p. 34). -/
inductive AVal (V : Type*) where
  | var (x : V)
  | new (ℓ : ℕ)
  | null
  deriving DecidableEq

variable {V : Type*}

/-- Andersen's constraints for one statement (notes p. 34). -/
def Stmt.Sat (pt : AVal V → Set (AVal V)) : Stmt V → Prop
  | .new x ℓ => .new ℓ ∈ pt (.var x)
  | .null x => .null ∈ pt (.var x)
  | .addr x y => .var y ∈ pt (.var x)
  | .copy x y => pt (.var y) ⊆ pt (.var x)
  | .load x y => ∀ z ∈ pt (.var y), pt z ⊆ pt (.var x)
  | .store x y => ∀ z ∈ pt (.var x), pt (.var y) ⊆ pt z

/-- Solutions for a program (a set of statements; order is irrelevant). -/
def solutions (P : List (Stmt V)) : Set (AVal V → Set (AVal V)) := {pt | ∀ s ∈ P, s.Sat pt}

/-- Andersen's analysis: the least solution. -/
def andersen (P : List (Stmt V)) : AVal V → Set (AVal V) := sInf (solutions P)

/-! ## Concrete semantics -/

/-- Concrete locations: variables and heap cells `(ℓ, k)`, the `k`th allocation at
site `ℓ`. -/
inductive Loc (V : Type*) where
  | var (x : V)
  | cell (ℓ k : ℕ)
  deriving DecidableEq

/-- A store maps each location to the location it points to (`none` is null or
uninitialised). The counter numbers allocations. -/
structure Store (V : Type*) where
  val : Loc V → Option (Loc V)
  next : ℕ

/-- The abstraction of a location. -/
def Loc.abs : Loc V → AVal V
  | .var x => .var x
  | .cell ℓ _ => .new ℓ

variable [DecidableEq V]

/-- Execute one statement. A load or store through null does nothing. -/
def Stmt.exec (σ : Store V) : Stmt V → Store V
  | .new x ℓ => ⟨Function.update σ.val (.var x) (some (.cell ℓ σ.next)), σ.next + 1⟩
  | .null x => { σ with val := Function.update σ.val (.var x) none }
  | .addr x y => { σ with val := Function.update σ.val (.var x) (some (.var y)) }
  | .copy x y => { σ with val := Function.update σ.val (.var x) (σ.val (.var y)) }
  | .load x y =>
    match σ.val (.var y) with
    | some l => { σ with val := Function.update σ.val (.var x) (σ.val l) }
    | none => σ
  | .store x y =>
    match σ.val (.var x) with
    | some l => { σ with val := Function.update σ.val l (σ.val (.var y)) }
    | none => σ

/-- Run a sequence of statements. -/
def run (σ : Store V) (ss : List (Stmt V)) : Store V := ss.foldl Stmt.exec σ

/-- The initial store: everything null. -/
def Store.init : Store V := ⟨fun _ => none, 0⟩

/-- The safety invariant: every pointer is described by the points-to sets. -/
def Inv (pt : AVal V → Set (AVal V)) (σ : Store V) : Prop :=
  ∀ l l', σ.val l = some l' → l'.abs ∈ pt l.abs

/-- Each statement preserves the invariant of a solution of its constraint. -/
theorem Stmt.exec_inv {pt : AVal V → Set (AVal V)} {s : Stmt V} (hs : s.Sat pt) {σ : Store V}
    (hσ : Inv pt σ) : Inv pt (Stmt.exec σ s) := sorry

/-- The alias test (notes p. 35): disjoint points-to sets mean no aliasing. -/
theorem noAlias_sound {P : List (Stmt V)} {x y : V}
    (hxy : andersen P (.var x) ∩ andersen P (.var y) = ∅)
    {ss : List (Stmt V)} (hss : ∀ s ∈ ss, s ∈ P) {l : Loc V}
    (hx : (run Store.init ss).val (.var x) = some l) : (run Store.init ss).val (.var y) ≠ some l := sorry

/-! ## Steensgaard's analysis -/

/-- A Steensgaard (unification) solution: abstract values are grouped into
classes, all members of a class point to the same class, and the constraints
hold. -/
structure SteensgaardSol (P : List (Stmt V)) where
  cls : Setoid (AVal V)
  pt : AVal V → Set (AVal V)
  pt_resp : ∀ a b, cls.r a b → pt a = pt b
  pt_closed : ∀ a b c, b ∈ pt a → cls.r b c → c ∈ pt a
  sat : pt ∈ solutions P

/-- Steensgaard's conditional join `τ₂ ⊴ τ₁` (Steensgaard 1996, §4): the
target of the source is empty (`⊥`, no pointer flows) or equal to the target of
the destination. -/
def CJoin {α : Type*} (s t : Set α) : Prop := s = ∅ ∨ s = t

theorem CJoin.subset {α : Type*} {s t : Set α} (h : CJoin s t) : s ⊆ t := sorry

/-- Steensgaard's constraints for one statement (Steensgaard 1996, Figure 3):
`x := y` gives `pt(y) ⊴ pt(x)`, `x := *y` gives `pt(z) ⊴ pt(x)` for each target
`z` of `y`, `*x := y` gives `pt(y) ⊴ pt(z)` for each target `z` of `x`, and the
address/allocation/null rules place the value in the target of `x`. -/
def Stmt.SteensSat (pt : AVal V → Set (AVal V)) : Stmt V → Prop
  | .new x ℓ => .new ℓ ∈ pt (.var x)
  | .null x => .null ∈ pt (.var x)
  | .addr x y => .var y ∈ pt (.var x)
  | .copy x y => CJoin (pt (.var y)) (pt (.var x))
  | .load x y => ∀ z ∈ pt (.var y), CJoin (pt z) (pt (.var x))
  | .store x y => ∀ z ∈ pt (.var x), CJoin (pt (.var y)) (pt z)

omit [DecidableEq V] in
/-- Steensgaard's constraints imply Andersen's. -/
theorem Stmt.SteensSat.sat {pt : AVal V → Set (AVal V)} :
    ∀ {s : Stmt V}, s.SteensSat pt → s.Sat pt := sorry

/-- A Steensgaard (unification) model of a program: an equivalence relation on
abstract values (Steensgaard's type variables), a points-to function constant
on classes whose value is empty or exactly one whole class (each type variable
has at most one target type variable), and the unification constraints. -/
structure SteensgaardModel (P : List (Stmt V)) where
  cls : Setoid (AVal V)
  pt : AVal V → Set (AVal V)
  pt_resp : ∀ a b, cls.r a b → pt a = pt b
  pt_closed : ∀ a b c, b ∈ pt a → cls.r b c → c ∈ pt a
  pt_single : ∀ a b c, b ∈ pt a → c ∈ pt a → cls.r b c
  sat : ∀ s ∈ P, s.SteensSat pt

omit [DecidableEq V] in
/-- Every Steensgaard model is an Andersen solution. -/
theorem SteensgaardModel.isAndersen {P : List (Stmt V)} (S : SteensgaardModel P) :
    S.pt ∈ solutions P := sorry

omit [DecidableEq V] in
/-- The skeleton's `SteensgaardSol` of a model. -/
def SteensgaardModel.toSol {P : List (Stmt V)} (S : SteensgaardModel P) :
    SteensgaardSol P where
  cls := S.cls
  pt := S.pt
  pt_resp := S.pt_resp
  pt_closed := S.pt_closed
  sat := S.isAndersen

omit [DecidableEq V] in
/-- Steensgaard is coarser than Andersen. -/
theorem SteensgaardModel.andersen_le {P : List (Stmt V)} (S : SteensgaardModel P) :
    andersen P ≤ S.pt := sorry

/-- Steensgaard's analysis is safe. -/
theorem SteensgaardModel.sound {P : List (Stmt V)} (S : SteensgaardModel P)
    {ss : List (Stmt V)} (hss : ∀ s ∈ ss, s ∈ P) {l l' : Loc V}
    (h : (run Store.init ss).val l = some l') : l'.abs ∈ S.pt l.abs := sorry

omit [DecidableEq V] in
/-- Every program has a Steensgaard model: one class, pointing to itself. -/
def SteensgaardModel.top (P : List (Stmt V)) : SteensgaardModel P where
  cls := ⟨fun _ _ => True, ⟨fun _ => trivial, fun _ => trivial, fun _ _ => trivial⟩⟩
  pt := fun _ => Set.univ
  pt_resp := fun _ _ _ => rfl
  pt_closed := fun _ _ _ _ _ => trivial
  pt_single := fun _ _ _ _ _ => trivial
  sat := by
    intro s _
    cases s <;> simp [Stmt.SteensSat, CJoin]

/-- `p = &a; q = &b; p = q`: unification merges the targets of `p` and `q`. -/
def unifyExample : List (Stmt String) := [.addr "p" "a", .addr "q" "b", .copy "p" "q"]

/-- The inclusion `andersen ≤ S.pt` is strict: Andersen finds `pt(q) = {b}`,
while in every Steensgaard model `q` may also point to `a`. -/
theorem steensgaard_strict :
    andersen unifyExample (.var "q") = {.var "b"} ∧
      ∀ S : SteensgaardModel unifyExample, AVal.var "a" ∈ S.pt (.var "q") := sorry

/-! ## The notes' example (p. 33) -/

/-- `p = &c; *p = 3; p = &d; q = &e` (the integer store does not involve pointers). -/
def notesExample : List (Stmt String) := [.addr "p" "c", .addr "p" "d", .addr "q" "e"]

/-- The least solution for `notesExample`. -/
def notesExamplePt (a : AVal String) : Set (AVal String) :=
  if a = .var "p" then {.var "c", .var "d"} else if a = .var "q" then {.var "e"} else ∅

/-- "The indirect write may update `c` or `d` but not `e`". -/
theorem notesExample_andersen :
    andersen notesExample (.var "p") = {.var "c", .var "d"} ∧
      andersen notesExample (.var "q") = {.var "e"} := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.PointsTo
