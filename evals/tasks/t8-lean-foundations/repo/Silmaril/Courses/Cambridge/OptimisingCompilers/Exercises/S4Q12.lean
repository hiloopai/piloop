module

public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q11

/-! # Exercise sheet 4, question 12 (WebLambda examples)

Source: `cam-optcomp-2025-exercise-sheet-4` in `references.bib`, question 12.


(a) `int →^{server} int`, created on the server only: the body runs only on the
server, so by (fun) so does the λ (`example_a`).

(b) The branches meet only at `int →^∅ int` (`S4Q10.if_example`), so the
application can run nowhere: its environments are `∅` (`example_b`), and no
derivation gives a non-empty set (`example_b_nowhere`). The program is useless
although each branch alone is fine.

(c) The left operand has type `int →^∅ int` and `λx.x : int →^𝓔 int`, so `⊕`
gives `int →^𝓔 int` (`example_c`): the identity covers both sides.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q12

open S4Q9 S4Q10 S4Q11

theorem example_a :
    DerivesOplus Γprim (.lam "x" (.app (.var "console.write") (.var "x")))
      (.arrow .int {.server} .int) {.server} := sorry

theorem example_b :
    DerivesOplus Γprim
      (.app (.ite (.var "b") (.var "console.write") (.var "window.alert")) (.var "b")) .int ∅ := sorry

/-! ### Inversion lemmas for `example_b_nowhere` -/

theorem example_b_nowhere {E : Set Loc}
    (h : DerivesOplus Γprim
      (.app (.ite (.var "b") (.var "console.write") (.var "window.alert")) (.var "b")) .int E) :
    E = ∅ := sorry

theorem example_c :
    DerivesOplus Γprim
      (.oplus (.ite (.var "b") (.var "console.write") (.var "window.alert")) (.lam "x" (.var "x")))
      (.arrow .int Set.univ .int) Set.univ := sorry

end Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q12
