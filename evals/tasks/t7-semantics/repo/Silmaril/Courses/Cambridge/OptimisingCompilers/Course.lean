module

-- Part A: classical dataflow optimisations (lectures 1–8)
public import Silmaril.Courses.Cambridge.OptimisingCompilers.ThreeAddress
public import Silmaril.Courses.Cambridge.OptimisingCompilers.BasicBlocks
public import Silmaril.Courses.Cambridge.OptimisingCompilers.UnreachableCode
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Liveness
public import Silmaril.Courses.Cambridge.OptimisingCompilers.AvailableExpressions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.DeadCode
public import Silmaril.Courses.Cambridge.OptimisingCompilers.DataflowAnomalies
public import Silmaril.Courses.Cambridge.OptimisingCompilers.RegisterAllocation
public import Silmaril.Courses.Cambridge.OptimisingCompilers.CommonSubexpression
public import Silmaril.Courses.Cambridge.OptimisingCompilers.CodeMotion
public import Silmaril.Courses.Cambridge.OptimisingCompilers.SSA
-- Part B: higher-level optimisations (lectures 8–13a)
public import Silmaril.Courses.Cambridge.OptimisingCompilers.StrengthReduction
public import Silmaril.Courses.Cambridge.OptimisingCompilers.AbstractInterpretation
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Strictness
public import Silmaril.Courses.Cambridge.OptimisingCompilers.ConstraintBased
public import Silmaril.Courses.Cambridge.OptimisingCompilers.ControlFlowAnalysis
public import Silmaril.Courses.Cambridge.OptimisingCompilers.InferenceBased
public import Silmaril.Courses.Cambridge.OptimisingCompilers.EffectSystems
public import Silmaril.Courses.Cambridge.OptimisingCompilers.PointsTo
-- Part C: instruction scheduling (lectures 14–15)
public import Silmaril.Courses.Cambridge.OptimisingCompilers.InstructionScheduling
-- Part D: decompilation (lectures 15–16)
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Decompilation
-- The course via institutions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Signature
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Assertions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Transitions
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Relational
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Institutions.Observational
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Passes
-- Exercise sheets 1–4
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q1
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q2
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S1Q3
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S2Q1
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S2Q2
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S2Q3
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q1
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q2
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S3Q3
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q1
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q2
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q3
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q4
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q5
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q6
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q7
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q8
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q9
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q10
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q11
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q12
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q13
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q14
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q15
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q16
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q17
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q18
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q19
public import Silmaril.Courses.Cambridge.OptimisingCompilers.Exercises.S4Q20

/-! # Cambridge Part II *Optimising Compilers* (Lent 2025)

Course developments, with section-level modules
and worked exercises. The primary source is `cam-optcomp-2025-handout` in
`references.bib`: notes on pages 1–41, slides on pages 42–131.

The shared dataflow framework is `Silmaril.Semantics.Dataflow`. The core route
is `ThreeAddress` → `Liveness` → `DeadCode`; institution views reuse those
semantics and correctness proofs. Not every exercise has a formal answer;
see the coverage table in `docs/courses/cambridge-optimising.md`.
-/
