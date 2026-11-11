---- MODULE wake ----
\* Who a queued lease wakes, and who takes it. Two workers of one slot each, two leases queued in order. L =
\* kit/leases.py and W = workers/worker.py in piloop/plugins/executor/ at this commit. A woken worker turns at once;
\* every worker also turns each renew period, and a turn acquires the oldest queued lease when it has a free slot. TLC
\* checks it (`tlc -config wake_fixed.cfg wake.tla`); tests/test_wake_model.py replays its graph against the code.
EXTENDS Naturals, FiniteSets
CONSTANTS TARGETED, TURNS, RECHECK
Ws == {"w1", "w2"}
Ls == {1, 2}
VARIABLES st, holder, target, woken, up, seen, grants, wakes, crashed, op, arg
vars == <<st, holder, target, woken, up, seen, grants, wakes, crashed, op, arg>>
Queued == {l \in Ls : st[l] = "queued"}
Free(w) == \A l \in Ls : ~(st[l] = "live" /\ holder[l] = w)
Oldest == CHOOSE l \in Queued : \A m \in Queued : l <= m
Init == /\ st = [l \in Ls |-> "none"] /\ holder = [l \in Ls |-> "-"] /\ target = [l \in Ls |-> "-"]
        /\ woken = {} /\ up = [w \in Ws |-> TRUE] /\ seen = [w \in Ws |-> TRUE] /\ grants = [l \in Ls |-> 0]
        /\ wakes = [l \in Ls |-> 0] /\ crashed = FALSE /\ op = "init" /\ arg = <<"-", 0>>
\* L:Leases.submit: TARGETED wakes one worker that the server saw lately, with a free slot and no wake of another
\* queued lease pending (the first by name), or none; else LeaseQueued wakes every worker.
Eligible == {w \in Ws : seen[w] /\ Free(w) /\ \A m \in Queued : target[m] # w}
Choice == IF "w1" \in Eligible THEN "w1" ELSE IF "w2" \in Eligible THEN "w2" ELSE "-"
Queue(l) == /\ st[l] = "none" /\ (l = 1 \/ st[1] # "none")
            /\ st' = [st EXCEPT ![l] = "queued"]
            /\ LET to == IF TARGETED THEN {Choice} \ {"-"} ELSE Ws IN
                 /\ target' = [target EXCEPT ![l] = IF TARGETED THEN Choice ELSE "all"]
                 /\ woken' = woken \cup {<<w, l>> : w \in to \cap {v \in Ws : up[v]}}
                 /\ wakes' = [wakes EXCEPT ![l] = Cardinality(to)]
            /\ op' = "queue" /\ arg' = <<"-", l>> /\ UNCHANGED <<holder, up, seen, grants, crashed>>
\* L:Leases.acquire under the write lock: the oldest queued lease, to a worker with a free slot. Without RECHECK, a
\* wake hands over its own lease whatever became of it (a push, not a pull).
Grant(w, l) == /\ st' = [st EXCEPT ![l] = "live"] /\ holder' = [holder EXCEPT ![l] = w]
               /\ grants' = [grants EXCEPT ![l] = @ + 1]
\* W:_listen: the Work message makes the worker turn now.
Wake(w, l) == /\ <<w, l>> \in woken /\ up[w] /\ woken' = woken \ {<<w, l>>}
              /\ IF RECHECK
                 THEN IF Free(w) /\ Queued # {} THEN Grant(w, Oldest) ELSE UNCHANGED <<st, holder, grants>>
                 ELSE IF Free(w) THEN Grant(w, l) ELSE UNCHANGED <<st, holder, grants>>
              /\ op' = "wake" /\ arg' = <<w, l>> /\ UNCHANGED <<target, up, seen, wakes, crashed>>
\* W:_run's loop: a turn each renew period, which acquires.
Turn(w) == /\ TURNS /\ up[w] /\ Free(w) /\ Queued # {} /\ Grant(w, Oldest)
           /\ op' = "turn" /\ arg' = <<w, Oldest>> /\ UNCHANGED <<target, woken, up, seen, wakes, crashed>>
\* The job ends and reports: its slot is free.
Finish(w) == /\ \E l \in Ls : st[l] = "live" /\ holder[l] = w
             /\ LET l == CHOOSE m \in Ls : st[m] = "live" /\ holder[m] = w IN
                  /\ st' = [st EXCEPT ![l] = "done"] /\ op' = "finish" /\ arg' = <<w, l>>
             /\ UNCHANGED <<holder, target, woken, up, seen, grants, wakes, crashed>>
\* One idle worker's process ends for good; the wakes that it did not read are lost.
Crash(w) == /\ up[w] /\ Free(w) /\ ~crashed /\ up' = [up EXCEPT ![w] = FALSE] /\ crashed' = TRUE
            /\ woken' = {p \in woken : p[1] # w} /\ op' = "crash" /\ arg' = <<w, 0>>
            /\ UNCHANGED <<st, holder, target, seen, grants, wakes>>
\* L:Leases.capacity's silence: the server stops counting a worker that sent nothing for grace + slack.
Silence(w) == /\ ~up[w] /\ seen[w] /\ seen' = [seen EXCEPT ![w] = FALSE] /\ op' = "silence" /\ arg' = <<w, 0>>
              /\ UNCHANGED <<st, holder, target, woken, up, grants, wakes, crashed>>
Done == /\ \A l \in Ls : st[l] = "done" /\ UNCHANGED vars
Next == \/ \E l \in Ls : Queue(l)
        \/ \E w \in Ws : \E l \in Ls : Wake(w, l)
        \/ \E w \in Ws : Turn(w) \/ Finish(w) \/ Crash(w) \/ Silence(w)
        \/ Done

\* Assumed: core queues each lease, live workers turn and read their wakes, and jobs end.
Live == /\ WF_vars(\E l \in Ls : Queue(l)) /\ \A w \in Ws : WF_vars(Finish(w))
        /\ \A w \in Ws : WF_vars(\E l \in Ls : Wake(w, l))
Spec == Init /\ [][Next]_vars /\ Live /\ \A w \in Ws : WF_vars(Turn(w))
\* Without the turns: EventuallyAcquired must fail, so it is not vacuous.
Unfair == Init /\ [][Next]_vars /\ Live

\* No lease is granted twice, and no worker holds more than its one slot.
NoDoubleGrant == /\ \A l \in Ls : grants[l] <= 1
                 /\ \A w \in Ws : Cardinality({l \in Ls : st[l] = "live" /\ holder[l] = w}) <= 1
\* A queued lease wakes at most one worker.
OneWakePerLease == \A l \in Ls : wakes[l] <= 1
\* A queued lease that a live worker can run is eventually acquired.
EventuallyAcquired == \A l \in Ls : (st[l] = "queued") ~> (st[l] # "queued")
====
