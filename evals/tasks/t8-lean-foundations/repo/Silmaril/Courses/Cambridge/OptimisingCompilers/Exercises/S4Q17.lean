module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.PointsTo

/-! # Exercise sheet 4, question 17 (may-alias versus must-alias)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 17.


Answer: points-to sets over-approximate, so they give *may*-alias information
only. If `pt(x) ∩ pt(y) = ∅` then `x` and `y` are never aliases
(`PointsTo.noAlias_sound`); a non-empty intersection does not imply an alias
occurs (`mayAlias_not_must`): in `x = &a; y = &b; y = &a` executed in the
order `y = &a; y = &b; x = &a`, the sets overlap at `a` but at the end `x` and
`y` differ.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q17

open PointsTo

/-- The example program. -/
def program : List (Stmt String) := [.addr "x" "a", .addr "y" "b", .addr "y" "a"]

/-- The points-to sets overlap, yet an execution ends with `x` and `y` pointing
to different locations. -/
theorem mayAlias_not_must :
    (andersen program (.var "x") ∩ andersen program (.var "y")).Nonempty ∧
      (run Store.init [.addr "y" "a", .addr "y" "b", .addr "x" "a"]).val (.var "x") ≠
        (run Store.init [.addr "y" "a", .addr "y" "b", .addr "x" "a"]).val (.var "y") := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q17
