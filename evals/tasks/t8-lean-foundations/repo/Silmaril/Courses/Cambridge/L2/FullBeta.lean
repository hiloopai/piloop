module

public import Silmaril.Courses.Cambridge.L2.CallByName

/-! # Cambridge L2 full beta reduction, §4.2

The rules on p. 57 allow either side of an application to reduce, perform beta
substitution before the argument is a value, and reduce inside a lambda.
`BetaStep` extends the call-by-name rules with those contexts, propagating the
new reductions through the other L2 constructs as well.
-/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L2

inductive BetaStep : Config → Config → Prop where
  | name {before after : Config} (step : NameStep before after) :
      BetaStep before after
  | binaryLeft {left left' right : Expr} {op : Op} {s s' : Store}
      (inner : BetaStep ⟨left, s⟩ ⟨left', s'⟩) :
      BetaStep ⟨.binary left op right, s⟩ ⟨.binary left' op right, s'⟩
  | binaryRight {left right right' : Expr} {op : Op} {s s' : Store}
      (value : IsValue left) (inner : BetaStep ⟨right, s⟩ ⟨right', s'⟩) :
      BetaStep ⟨.binary left op right, s⟩ ⟨.binary left op right', s'⟩
  | assignStep {loc : Loc} {e e' : Expr} {s s' : Store}
      (inner : BetaStep ⟨e, s⟩ ⟨e', s'⟩) :
      BetaStep ⟨.assign loc e, s⟩ ⟨.assign loc e', s'⟩
  | seqStep {left left' right : Expr} {s s' : Store}
      (inner : BetaStep ⟨left, s⟩ ⟨left', s'⟩) :
      BetaStep ⟨.seq left right, s⟩ ⟨.seq left' right, s'⟩
  | ifStep {test test' yes no : Expr} {s s' : Store}
      (inner : BetaStep ⟨test, s⟩ ⟨test', s'⟩) :
      BetaStep ⟨.cond test yes no, s⟩ ⟨.cond test' yes no, s'⟩
  | appLeft {function function' argument : Expr} {s s' : Store}
      (inner : BetaStep ⟨function, s⟩ ⟨function', s'⟩) :
      BetaStep ⟨.app function argument, s⟩ ⟨.app function' argument, s'⟩
  | appRight {function argument argument' : Expr} {s s' : Store}
      (inner : BetaStep ⟨argument, s⟩ ⟨argument', s'⟩) :
      BetaStep ⟨.app function argument, s⟩ ⟨.app function argument', s'⟩
  | lamBody {domain : Ty} {body body' : Expr} {s s' : Store}
      (inner : BetaStep ⟨body, s⟩ ⟨body', s'⟩) :
      BetaStep ⟨.lam domain body, s⟩ ⟨.lam domain body', s'⟩
  | letStep {type : Ty} {value value' body : Expr} {s s' : Store}
      (inner : BetaStep ⟨value, s⟩ ⟨value', s'⟩) :
      BetaStep ⟨.letE type value body, s⟩ ⟨.letE type value' body, s'⟩

abbrev BetaSteps : Config → Config → Prop := Relation.ReflTransGen BetaStep

theorem valueSteps_imply_beta {before after : Config}
    (steps : Steps before after) : BetaSteps before after := sorry

theorem nameSteps_imply_beta {before after : Config}
    (steps : NameSteps before after) : BetaSteps before after := sorry

/-- Full beta can reduce a lambda's body while call-by-value treats it as a value. -/
theorem reduces_inside_lambda (s : Store) :
    BetaStep
      ⟨.lam .int (.binary (.int 1) .add (.int 2)), s⟩
      ⟨.lam .int (.int 3), s⟩ := sorry

theorem value_cannot_reduce_inside_lambda (s : Store) :
    ¬ Step
      ⟨.lam .int (.binary (.int 1) .add (.int 2)), s⟩
      ⟨.lam .int (.int 3), s⟩ := sorry

end Silmaril.Courses.Cambridge.L2
