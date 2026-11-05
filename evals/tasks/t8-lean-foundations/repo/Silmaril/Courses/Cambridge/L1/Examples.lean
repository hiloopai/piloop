module

public import Silmaril.Courses.Cambridge.L1.Reduction

/-! # The three-step store update on the notes p. 14 -/

@[expose] public section

namespace Silmaril.Courses.Cambridge.L1.Examples

def initialStore : Store := (∅ : Store).insert "l" 3

def start : Config := ⟨.assign "l" (.binary (.int 2) .add (.deref "l")), initialStore⟩
def afterRead : Config := ⟨.assign "l" (.binary (.int 2) .add (.int 3)), initialStore⟩
def afterAdd : Config := ⟨.assign "l" (.int 5), initialStore⟩
def finished : Config := ⟨.skip, initialStore.insert "l" 5⟩

theorem trace : lts.MTr start [(), (), ()] finished := sorry

theorem final_lookup : Store.lookup finished.store "l" = some 5 := sorry

theorem final_terminal : Terminal finished := sorry

end Silmaril.Courses.Cambridge.L1.Examples
