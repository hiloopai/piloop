---- MODULE first_transfers ----
\* First transfers as rows (#821 at 280b9f80c); paths under piloop/plugins/executor/kit/ unless named. One repo, KIDS
\* requests (`send` in the API processes), the daemon's consumer `run` and its build threads. `age` is 1 once the
\* row is past its deadline from started_at. An ask makes other reads stale (`_state` compares started_at). Arguments: what a step changed.
EXTENDS Naturals, FiniteSets
CONSTANTS WAKE_ON_UNSETTLED, START_IF_REQUESTED, RECHECK, ONE_BUILD, KIDS
\* Fixes: an end that was not written wakes the consumer; `run` builds only if its update found the row requested.
\* Protections: an ask writes only over the row that its request read (transfers.py:132); no second build (:192).
Kids == 1..KIDS  Threads == {1, 2}
VARIABLES row, age, stale, kid, seen, pc, snap, lag, building, unsettled, th, wake, due, crashes, flags
vars == <<row, age, stale, kid, seen, pc, snap, lag, building, unsettled, th, wake, due, crashes, flags>>
daemon == <<pc, snap, lag, building, unsettled, th, wake, due, crashes>>
Init == /\ row = "none" /\ age = 0 /\ stale = {} /\ kid = [c \in Kids |-> "ask"] /\ seen = [c \in Kids |-> "none"]
        /\ pc = "idle" /\ snap = <<FALSE, FALSE>> /\ lag = 0 /\ building = FALSE /\ unsettled = FALSE /\ th = [i \in Threads |-> "idle"]
        /\ wake = TRUE /\ due = FALSE /\ crashes = 0 /\ flags = {}
\* transfers.py:117-129. A read with no transaction: built and in the CAS answers; requested waits, or fails once
\* past its deadline; none, failed, or a blob that the CAS lost goes on to ask.
Read(c) == /\ kid[c] \in {"ask", "wait"} /\ seen' = [seen EXCEPT ![c] = IF row \in {"built", "requested"} THEN "none" ELSE row]
           /\ kid' = [kid EXCEPT ![c] = CASE row = "built" -> "ok"
                                          [] row = "requested" -> (IF age = 1 THEN "failed" ELSE "wait")
                                          [] OTHER -> "write"]
           /\ UNCHANGED <<row, age, stale, flags, daemon>>
\* transfers.py:130-138, 146-152. Under the write lock: ask if the row is as it was read, with the event that wakes
\* the consumer, and raise a failed one after asking. A row that changed meanwhile: wait.
Write(c) == LET ask == ~RECHECK \/ (row = seen[c] /\ c \notin stale) IN
            /\ kid[c] = "write" /\ seen' = [seen EXCEPT ![c] = "none"]
            /\ IF ask THEN row' = "requested" /\ age' = 0 /\ stale' = {k \in Kids \ {c} : kid[k] = "write"} /\ wake' = TRUE
                          /\ flags' = flags \cup (IF row = "built" THEN {"overbuilt"} ELSE {})
                      ELSE stale' = stale \ {c} /\ UNCHANGED <<row, age, wake, flags>>
            /\ kid' = [kid EXCEPT ![c] = IF ask /\ seen[c] = "failed" THEN "failed" ELSE "wait"]
            /\ UNCHANGED <<pc, snap, lag, building, unsettled, th, due, crashes>>
\* daemon/main.py:218-223, daemon/loop.py:104-108. The daemon restarts: an exception of a consumer ends it, and its
\* memory and threads go. The new one runs every consumer once. Not fair, at most once.
Restarted == /\ crashes < 1 /\ crashes' = crashes + 1 /\ building' = FALSE /\ unsettled' = FALSE /\ pc' = "idle"
             /\ th' = [i \in Threads |-> "idle"] /\ wake' = TRUE /\ due' = FALSE /\ snap' = <<FALSE, FALSE>> /\ lag' = 0
Restart == Restarted /\ UNCHANGED <<row, age, stale, kid, seen, flags>>
\* transfers.py:186-190, daemon/loop.py:146-152. A turn that an event, the start or the due woke reads the rows.
RunRead == /\ pc = "idle" /\ wake /\ wake' = FALSE /\ snap' = <<row = "requested", age = 1>> /\ pc' = "read"
           /\ UNCHANGED <<row, age, stale, kid, seen, lag, building, unsettled, th, due, crashes, flags>>
\* transfers.py:191-202, 216-217, daemon/loop.py:199. Under the process's lock: an end that was not written and is
\* past its deadline is written failed; a requested row that no thread builds is new. Else the turn ends, with a
\* due at the deadline of a row that it read requested, if that is ahead. A write that raises ends the daemon.
RunLock(ok) == LET late == snap[1] /\ unsettled /\ snap[2]
                   new == snap[1] /\ (~building \/ ~ONE_BUILD) /\ ~unsettled IN
  /\ pc = "read"
  /\ \/ late /\ ok /\ row' = "failed" /\ unsettled' = FALSE /\ due' = FALSE /\ pc' = "idle" /\ snap' = <<FALSE, FALSE>>
        /\ UNCHANGED <<lag, building, th, wake, crashes>>
     \/ late /\ ~ok /\ Restarted /\ UNCHANGED row
     \/ ~late /\ new /\ ok /\ building' = TRUE /\ pc' = "start" /\ UNCHANGED <<row, lag, unsettled, th, wake, due, crashes, snap>>
     \/ ~late /\ ~new /\ ok /\ due' = (snap[1] /\ ~snap[2]) /\ pc' = "idle" /\ snap' = <<FALSE, FALSE>>
        /\ UNCHANGED <<row, lag, building, unsettled, th, wake, crashes>>
  /\ UNCHANGED <<age, stale, kid, seen, flags>>
\* transfers.py:203-217. Set started_at to the time read after the lock (`lag` ticks ago) where the row is still
\* requested, then start a thread; the fix starts one only if that update found the row. The due is the new deadline. A write that raises ends the daemon.
RunStart(ok) == LET found == row = "requested"
                    i == CHOOSE j \in Threads : th[j] = "idle" IN
  /\ pc = "start" /\ \E j \in Threads : th[j] = "idle"
  /\ \/ ok /\ age' = (IF found THEN lag ELSE age) /\ lag' = 0 /\ due' = TRUE /\ pc' = "idle" /\ snap' = <<FALSE, FALSE>>
        /\ (IF found \/ ~START_IF_REQUESTED
            THEN th' = [th EXCEPT ![i] = "building"] /\ flags' = flags \cup (IF found THEN {} ELSE {"unasked"})
                 /\ UNCHANGED building
            ELSE building' = FALSE /\ UNCHANGED <<th, flags>>)
        /\ UNCHANGED <<unsettled, wake, crashes>>
     \/ ~ok /\ Restarted /\ UNCHANGED <<age, flags>>
  /\ UNCHANGED <<row, stale, kid, seen>>
\* transfers.py:221-229. The repo's head goes into the CAS, or that raises. It can take longer than its deadline.
BuildEnd(i, ok) == /\ th[i] = "building" /\ th' = [th EXCEPT ![i] = IF ok THEN "ok" ELSE "fail"]
                   /\ UNCHANGED <<row, age, stale, kid, seen, pc, snap, lag, building, unsettled, wake, due, crashes, flags>>
\* transfers.py:230-238, 240-251. Write the end and its event, then leave `_building`. A write that raises leaves the
\* row requested, and the repo unsettled.
Settle(i, ok) == /\ th[i] \in {"ok", "fail"} /\ th' = [th EXCEPT ![i] = "idle"] /\ building' = FALSE
                 /\ IF ok THEN row' = (IF th[i] = "ok" THEN "built" ELSE "failed") /\ UNCHANGED <<unsettled, wake>>
                          ELSE unsettled' = TRUE /\ wake' = (wake \/ WAKE_ON_UNSETTLED) /\ UNCHANGED row
                 /\ UNCHANGED <<age, stale, kid, seen, pc, snap, lag, due, crashes, flags>>
\* transfers.py:120. The CAS lost the built blob (once, not fair). Time passes while a row is requested.
Lose == /\ row = "built" /\ "lost" \notin flags /\ row' = "lost" /\ flags' = flags \cup {"lost"}
        /\ UNCHANGED <<age, stale, kid, seen, daemon>>
Tick == /\ row = "requested" /\ age = 0 /\ age' = 1 /\ lag' = (IF pc = "start" THEN 1 ELSE lag)
        /\ UNCHANGED <<row, stale, kid, seen, flags, pc, snap, building, unsettled, th, wake, due, crashes>>
\* daemon/loop.py:147, 165-167. The loop runs the consumer at its due. Done: every request answered, nothing pending.
DueFire == /\ due /\ (row # "requested" \/ age = 1) /\ due' = FALSE /\ wake' = TRUE
           /\ UNCHANGED <<row, age, stale, kid, seen, pc, snap, lag, building, unsettled, th, crashes, flags>>
Done == /\ \A c \in Kids : kid[c] \in {"ok", "failed"} /\ row # "requested" /\ \A i \in Threads : th[i] = "idle"
        /\ pc = "idle" /\ ~wake /\ ~due /\ ~unsettled /\ UNCHANGED vars
Next == \/ \E c \in Kids : Read(c) \/ Write(c)
        \/ RunRead \/ \E ok \in BOOLEAN : RunLock(ok) \/ RunStart(ok)
        \/ \E i \in Threads, ok \in BOOLEAN : BuildEnd(i, ok) \/ Settle(i, ok)
        \/ Lose \/ Tick \/ DueFire \/ Restart \/ Done
\* Assumed: requests read again, the loop runs the consumer, time passes, a write eventually succeeds, and each
\* build ends (`FairBuild`). Restarts and lost blobs are not fair.
Fair == /\ \A c \in Kids : WF_vars(Read(c)) /\ WF_vars(Write(c))
        /\ WF_vars(RunRead) /\ WF_vars(RunLock(TRUE)) /\ WF_vars(RunStart(TRUE)) /\ WF_vars(Tick) /\ WF_vars(DueFire)
        /\ \A i \in Threads : WF_vars(Settle(i, TRUE))
FairBuild == \A i \in Threads : WF_vars(\E ok \in BOOLEAN : BuildEnd(i, ok))
Spec == Init /\ [][Next]_vars /\ Fair /\ FairBuild  UnfairBuildSpec == Init /\ [][Next]_vars /\ Fair

OneBuild == Cardinality({i \in Threads : th[i] # "idle"}) <= 1
NoUnaskedBuild == "unasked" \notin flags  NoAskOverBuilt == "overbuilt" \notin flags
RowSettles == (row = "requested") ~> (row # "requested")  EveryRequestSettles == \A c \in Kids : <>(kid[c] \in {"ok", "failed"})
\* The graphs of the tests: one request and no restart or lost blob; two and no deadline either; one and no deadline.
NoFaults == crashes = 0 /\ "lost" \notin flags  TwoRequests == NoFaults /\ age = 0  Restarts == age = 0
====
