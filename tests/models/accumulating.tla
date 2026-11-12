---- MODULE accumulating ----
\* The accumulating campaign type's trunk. Paths are relative to the repo root; the driver is
\* tests/daemon/test_accumulating_model.py, which replays the graphs of the accumulating_traces_*.cfg configs.
\* Submissions wait; one merge job batches every waiting one onto the trunk, checks only the merge (decision 6), and
\* accepts it when the check is ok and the primary is at least the trunk's (decision 7). The primary is "how much work
\* the trunk holds", so a merge never lowers it; a bad submission is one whose merge fails the check.
EXTENDS Naturals, FiniteSets, Sequences

CONSTANTS Runs,       \* the runs, one submission each (and its rebases)
          Bad,        \* runs whose work fails the check
          Clash,      \* unordered pairs {a, b} of runs whose work conflicts as text
          Checkers,   \* merge jobs that may be in flight at once: 1 in the design
          CAS,        \* an accept checks that the trunk is still the merge's base
          SOLO,       \* a failed batch's members are retried one at a time
          MaxRebase,  \* how often a refused worker rebases and submits again
          MERGE_SETTLES \* an accepted merge settles the idea of a run that already ended

VARIABLES sub, why, ended, idea, trunk, resolved, rebases, jobs, solo, at, clock, op, arg
vars == <<sub, why, ended, idea, trunk, resolved, rebases, jobs, solo, at, clock, op, arg>>

Waiting == {r \in Runs : sub[r] = "waiting"}
\* r's work conflicts with work of S that it was not rebased onto.
Clashes(r, S) == \E t \in S \ resolved[r] : {r, t} \in Clash
\* What a refusal does to a run's idea: an ended run's idea opens again, with a note (accumulating/__init__.py:41).
Reopened(r) == IF ended[r] THEN "open" ELSE idea[r]
\* The submissions of S, oldest first: the order that a merge takes them in.
RECURSIVE Oldest(_)
Oldest(S) == IF S = {} THEN <<>>
             ELSE LET m == CHOOSE x \in S : \A y \in S : at[x] <= at[y] IN <<m>> \o Oldest(S \ {m})
\* Merge each of the sequence s in turn onto the trunk and acc: one that conflicts with them is left out.
RECURSIVE Taken(_, _)
Taken(s, acc) == IF s = <<>> THEN acc
                 ELSE Taken(Tail(s), IF Clashes(Head(s), trunk \cup acc) THEN acc ELSE acc \cup {Head(s)})

Init == /\ sub = [r \in Runs |-> "none"] /\ why = [r \in Runs |-> "none"]
        /\ ended = [r \in Runs |-> FALSE] /\ idea = [r \in Runs |-> "claimed"]
        /\ trunk = {} /\ resolved = [r \in Runs |-> {}] /\ rebases = [r \in Runs |-> 0]
        /\ jobs = {} /\ solo = {} /\ at = [r \in Runs |-> 0] /\ clock = 0 /\ op = "init" /\ arg = {}

\* piloop/plugins/campaign_type/kit/evaluation/submissions.py:141 submit. The accumulating planner scores no
\* submission of its own (piloop/plugins/campaign_type/accumulating/planner.py:33): only the merge is measured.
Submit(r) == /\ sub[r] = "none" /\ ~ended[r]
             /\ sub' = [sub EXCEPT ![r] = "waiting"]
             /\ at' = [at EXCEPT ![r] = clock] /\ clock' = clock + 1
             /\ op' = "submit" /\ arg' = {r}
             /\ UNCHANGED <<why, ended, idea, trunk, resolved, rebases, jobs, solo>>

\* The run ends. piloop/plugins/campaign_type/kit/runs/controller.py:1220 waits until its submissions are decided, then
\* piloop/plugins/campaign_type/accumulating/__init__.py:41 settles its idea: done when merged, else open. A run whose
\* submission still waits keeps its idea claimed here; Finish (or a refusal in StartBatch) settles it then.
EndRun(r) == /\ ~ended[r]
             /\ ended' = [ended EXCEPT ![r] = TRUE]
             /\ idea' = [idea EXCEPT ![r] = CASE sub[r] = "merged" -> "done"
                                               [] sub[r] \in {"none", "refused"} -> "open"
                                               [] OTHER -> "claimed"]
             /\ op' = "end" /\ arg' = {r}
             /\ UNCHANGED <<sub, why, trunk, resolved, rebases, jobs, solo, at, clock>>

\* piloop/plugins/campaign_type/accumulating/trunk.py:93 start, from the merge job of
\* piloop/plugins/campaign_type/accumulating/planner.py:46 (one per campaign: Checkers = 1). Every waiting submission,
\* oldest first, merges onto the trunk in turn; one that conflicts with the trunk or an earlier one is refused at once
\* (trunk.py:109). After a failed batch, the oldest one that goes alone goes by itself (SOLO; trunk.py:86).
StartBatch ==
  LET soloing == SOLO /\ solo \cap Waiting # {}
      pool == IF soloing THEN {Head(Oldest(solo \cap Waiting))} ELSE Waiting
      B == Taken(Oldest(pool), {})
      refused == pool \ B
  IN /\ Cardinality(jobs) < Checkers
     /\ pool # {}
     /\ sub' = [r \in Runs |-> IF r \in B THEN "checking" ELSE IF r \in refused THEN "refused" ELSE sub[r]]
     /\ why' = [r \in Runs |-> IF r \in refused THEN "conflict" ELSE why[r]]
     /\ idea' = [r \in Runs |-> IF r \in refused THEN Reopened(r) ELSE idea[r]]
     /\ jobs' = IF B = {} THEN jobs ELSE jobs \cup {[base |-> trunk, batch |-> B]}
     /\ op' = "batch" /\ arg' = B
     /\ UNCHANGED <<ended, trunk, resolved, rebases, solo, at, clock>>

\* The merge is measured once (piloop/plugins/campaign_type/kit/runs/controller.py:838 measure_once), then
\* piloop/plugins/campaign_type/accumulating/trunk.py:130 decide: taken when it scored and its primary is at least the
\* trunk's (the primary never drops here: see the header), and the trunk is still its base (CAS).
Finish(j) ==
  LET ok == j.batch \cap Bad = {}
      current == ~CAS \/ j.base = trunk
  IN /\ jobs' = jobs \ {j}
     /\ op' = "finish" /\ arg' = j.batch
     /\ CASE ok /\ current ->
               /\ trunk' = j.base \cup j.batch
               /\ sub' = [r \in Runs |-> IF r \in j.batch THEN "merged" ELSE sub[r]]
               /\ idea' = [r \in Runs |-> IF r \in j.batch /\ ended[r] /\ MERGE_SETTLES THEN "done" ELSE idea[r]]
               /\ solo' = solo \ j.batch
               /\ UNCHANGED why
          [] ok ->  \* the trunk moved meanwhile: merge them again onto the new trunk
               /\ sub' = [r \in Runs |-> IF r \in j.batch THEN "waiting" ELSE sub[r]]
               /\ UNCHANGED <<trunk, idea, solo, why>>
          [] SOLO /\ Cardinality(j.batch) > 1 ->  \* trunk.py:157: a failed batch's members go one at a time
               /\ sub' = [r \in Runs |-> IF r \in j.batch THEN "waiting" ELSE sub[r]]
               /\ solo' = solo \cup j.batch
               /\ UNCHANGED <<trunk, idea, why>>
          [] OTHER ->  \* trunk.py:170 refuse: the worker hears why, and the trunk to merge
               /\ sub' = [r \in Runs |-> IF r \in j.batch THEN "refused" ELSE sub[r]]
               /\ why' = [r \in Runs |-> IF r \in j.batch THEN "check" ELSE why[r]]
               /\ idea' = [r \in Runs |-> IF r \in j.batch THEN Reopened(r) ELSE idea[r]]
               /\ solo' = solo \ j.batch
               /\ UNCHANGED trunk
     /\ UNCHANGED <<ended, resolved, rebases, at, clock>>

\* The worker merges the trunk that its refusal names into its work (trunk.py:170), and submits again
\* (submissions.py:141; a tree that it submitted already is that submission, answered as it was).
Rebase(r) == /\ sub[r] = "refused" /\ ~ended[r] /\ rebases[r] < MaxRebase
             \* a merge of a trunk that the work holds changes nothing: the same tree is the same submission, decided
             /\ trunk \ (resolved[r] \cup {r}) # {}
             /\ sub' = [sub EXCEPT ![r] = "waiting"]
             /\ resolved' = [resolved EXCEPT ![r] = trunk]
             /\ rebases' = [rebases EXCEPT ![r] = @ + 1]
             /\ at' = [at EXCEPT ![r] = clock] /\ clock' = clock + 1
             /\ op' = "rebase" /\ arg' = {r}
             /\ UNCHANGED <<why, ended, idea, trunk, jobs, solo>>

\* The end: every run ended, and no submission waits or is checked.
Done == /\ \A r \in Runs : ended[r] /\ sub[r] \notin {"waiting", "checking"}
        /\ jobs = {}
        /\ UNCHANGED vars

Next == \/ \E r \in Runs : Submit(r) \/ EndRun(r) \/ Rebase(r)
        \/ StartBatch \/ \E j \in jobs : Finish(j)
        \/ Done

\* Assumed: every run eventually ends, and the merge job keeps running. A worker may never submit or rebase.
Spec == Init /\ [][Next]_vars
        /\ \A r \in Runs : WF_vars(EndRun(r))
        /\ WF_vars(StartBatch) /\ WF_vars(\E j \in jobs : Finish(j))

\* A negative control for the "eventually" rules: the merge job may stop finishing its checks.
UnfairSpec == Init /\ [][Next]_vars /\ \A r \in Runs : WF_vars(EndRun(r)) /\ WF_vars(StartBatch)

\* Only checked work lands: the check of the merge catches a bad submission, also in a batch with good ones.
TrunkOnlyGood == trunk \cap Bad = {}
\* A merged submission is in the trunk, and an idea is done only when its work is merged.
MergedIsInTrunk == \A r \in Runs : sub[r] = "merged" <=> r \in trunk
DoneMeansMerged == \A r \in Runs : idea[r] = "done" => r \in trunk
\* A good submission is never refused for a check that another member failed.
RefusedForItsOwnCheck == \A r \in Runs : why[r] = "check" => r \in Bad
\* The trunk never loses work it accepted (concurrent accepts).
TrunkGrows == [][trunk \subseteq trunk']_trunk
\* Every ended run's idea is eventually settled: done or open, never left claimed.
EventuallySettled == \A r \in Runs : <>[](ended[r] => idea[r] \in {"done", "open"})
\* A good submission that conflicts with nothing eventually lands.
GoodWorkLands == \A r \in Runs \ Bad :
                   (\A t \in Runs : {r, t} \notin Clash) => [](sub[r] = "waiting" => <>(r \in trunk))
====
