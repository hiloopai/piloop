---- MODULE api_tier ----
\* The daemon's API processes (#822): a leader, 2 children, their pipes, respawn, stop, close, leader death.
\* C = piloop/daemon/tier.py at bd1bfe597 (#822 before its fix). F = that file at 1bbc871a9, its fix: a ready pipe, a control pipe
\* as the only stop, `_stopping` read under `_lock`. Child i has generations 0..MAXG; a slot is <<i, gen>>.
EXTENDS Naturals, Sequences
CONSTANTS FIXED, CHECK, ONCE, AWAIT, KILL, MAXG, RESPAWNS
VARIABLES lpc, stopReq, stopping, started, dead, cur, snap, gen, proc, said, term, wpc, fails, firstReady, ctl, rdy, op, arg
vars == <<lpc, stopReq, stopping, started, dead, cur, snap, gen, proc, said, term, wpc, fails, firstReady, ctl, rdy, op, arg>>
Idx == {0, 1}
Slots == Idx \X (0..MAXG)
Live(i) == proc[i] \in {"starting", "serving", "hung"}
Shut(c, s) == IF FIXED /\ ONCE /\ c[s] > 0 THEN c ELSE [c EXCEPT ![s] = @ + 1]   \* F:188 `_close_control` closes once
Keep(v) == UNCHANGED v
Init == /\ lpc = "init" /\ stopReq = FALSE /\ stopping = FALSE /\ started = FALSE /\ dead = FALSE /\ cur = 0
        /\ snap = [i \in Idx |-> 0] /\ gen = [i \in Idx |-> 0] /\ proc = [i \in Idx |-> "none"]
        /\ said = [i \in Idx |-> FALSE] /\ term = [i \in Idx |-> FALSE] /\ wpc = [i \in Idx |-> "none"]
        /\ fails = [i \in Idx |-> 0] /\ firstReady = {} /\ ctl = [s \in Slots |-> 0] /\ rdy = [s \in Slots |-> 0]
        /\ op = "init" /\ arg = 0
\* C:70 starts each child and its watcher and returns. F:111 first waits for each first child's ready line.
Start == /\ lpc = "init" /\ proc' = [i \in Idx |-> "starting"] /\ op' = "start" /\ arg' = 0
         /\ IF FIXED /\ AWAIT THEN lpc' = "await" /\ Keep(<<started, wpc>>)
            ELSE lpc' = "serving" /\ started' = TRUE /\ wpc' = [i \in Idx |-> "wait"]
         /\ Keep(<<stopReq, stopping, dead, cur, snap, gen, said, term, fails, firstReady, ctl, rdy>>)
\* F:194 `_await_ready`: the ready line, else EOF or `api_start` passing (F:204); F:125 then closes and raises.
Await == /\ lpc = "await" /\ ~dead /\ op' = (IF said[cur] THEN "ready" ELSE "not_ready") /\ arg' = cur
         /\ IF ~said[cur] THEN lpc' = "abort" /\ stopReq' = TRUE /\ Keep(<<started, wpc, cur>>)
            ELSE IF cur = 1 THEN lpc' = "serving" /\ started' = TRUE /\ wpc' = [i \in Idx |-> "wait"] /\ Keep(<<stopReq, cur>>)
            ELSE cur' = 1 /\ Keep(<<lpc, stopReq, started, wpc>>)
         /\ Keep(<<stopping, dead, snap, gen, proc, said, term, fails, firstReady, ctl, rdy>>)
\* A child (C:143, F:260) writes its ready line once it serves (F:284), fails at start, hangs there, or crashes.
Child(i, to, n) == /\ proc' = [proc EXCEPT ![i] = to] /\ op' = n /\ arg' = i
                /\ Keep(<<lpc, stopReq, stopping, started, dead, cur, snap, gen, term, wpc, fails, ctl, rdy>>)
Ready(i) == /\ proc[i] = "starting" /\ ~dead /\ said' = [said EXCEPT ![i] = TRUE] /\ Child(i, "serving", "child_ready")
            /\ firstReady' = IF gen[i] = 0 THEN firstReady \cup {i} ELSE firstReady
Fail(i) == proc[i] \in {"starting", "serving"} /\ gen[i] < MAXG /\ Child(i, "exited", "child_fail") /\ Keep(<<said, firstReady>>)
Hang(i) == proc[i] = "starting" /\ Child(i, "hung", "child_hang") /\ Keep(<<said, firstReady>>)
\* C:161 ends on SIGTERM or on EOF of its pipe. F:286 ignores SIGTERM and ends on EOF once it serves; F:289 a write to
\* a dead leader's ready pipe fails, so a starting child of a dead leader exits.
Ends(i) == \/ (dead /\ proc[i] \in {"starting", "serving"})
           \/ (FIXED /\ proc[i] = "serving" /\ ctl[<<i, gen[i]>>] > 0)
           \/ (~FIXED /\ proc[i] \in {"starting", "serving"} /\ (term[i] \/ ctl[<<i, gen[i]>>] > 0))
End(i) == Ends(i) /\ Child(i, "exited", "child_end") /\ Keep(<<said, firstReady>>)
\* The watcher (C:127, F:223): wait for the exit, wait RESPAWN unless stopped, close the old pipes, spawn, await it.
W(i, from, to, n) == wpc[i] = from /\ ~dead /\ wpc' = [wpc EXCEPT ![i] = to] /\ op' = n /\ arg' = i
WExit(i) == proc[i] = "exited" /\ W(i, "wait", IF stopping THEN "done" ELSE "pause", "w_exit") /\ Keep(<<fails, stopReq>>)
WWoken(i) == stopping /\ W(i, "pause", "done", "w_woken") /\ Keep(<<fails, stopReq>>)
WTimeout(i) == ~stopping /\ W(i, "pause", "spawn", "w_timeout") /\ Keep(<<fails, stopReq>>)
\* F:254 `failed`: RESPAWNS children in a row that never served stop the daemon.
WAwait(i) == /\ fails' = [fails EXCEPT ![i] = IF said[i] THEN 0 ELSE @ + 1]
             /\ W(i, "await", IF ~said[i] /\ (stopping \/ fails'[i] >= RESPAWNS) THEN "done" ELSE "wait", "w_await")
             /\ stopReq' = (stopReq \/ (~said[i] /\ ~stopping /\ fails'[i] >= RESPAWNS))
\* C:138 closes the old pipe and spawns under `_lock`; F:240 first reads `_stopping` under it.
WSpawn(i) == LET s == <<i, gen[i]>> IN
  IF FIXED /\ CHECK /\ stopping THEN W(i, "spawn", "done", "w_spawn") /\ Keep(<<fails, stopReq, gen, proc, said, term, ctl, rdy>>)
  ELSE /\ W(i, "spawn", IF FIXED THEN "await" ELSE "wait", "w_spawn") /\ ctl' = Shut(ctl, s) /\ gen' = [gen EXCEPT ![i] = @ + 1]
       /\ rdy' = (IF FIXED THEN [rdy EXCEPT ![s] = @ + 1] ELSE rdy) /\ proc' = [proc EXCEPT ![i] = "starting"]
       /\ said' = [said EXCEPT ![i] = FALSE] /\ term' = [term EXCEPT ![i] = FALSE] /\ Keep(<<fails, stopReq>>)
WKeep == Keep(<<lpc, stopping, started, dead, cur, snap, firstReady>>)
WSteps(i) == \/ (WExit(i) \/ WWoken(i) \/ WTimeout(i) \/ WAwait(i)) /\ Keep(<<gen, proc, said, term, ctl, rdy>>) /\ WKeep
             \/ WSpawn(i) /\ WKeep
\* Daemon.shutdown: stop() (C:84 SIGTERMs, F:136 closes each control pipe), then close() (C:93, F:144), whose stop()
\* comes again. A start that fails (F:125) calls close() alone.
L(from, to, n) == lpc = from /\ ~dead /\ lpc' = to /\ op' = n /\ arg' = cur
Stop(from, to) == /\ L(from, to, "stop") /\ stopping' = TRUE /\ term' = (IF FIXED THEN term ELSE [i \in Idx |-> TRUE])
                  /\ ctl' = (IF FIXED THEN [s \in Slots |-> IF s[2] = gen[s[1]] THEN Shut(ctl, s)[s] ELSE ctl[s]] ELSE ctl)
                  /\ Keep(<<stopReq, started, dead, cur, snap, gen, proc, said, wpc, fails, firstReady, rdy>>)
Snap == L("stopped", "wait", "snapshot") /\ snap' = gen /\ cur' = 0
        /\ Keep(<<stopReq, stopping, started, dead, gen, proc, said, term, wpc, fails, firstReady, ctl, rdy>>)
\* C:101 / F:153 wait for each slot of the snapshot, and kill it after the timeout; C:105 closes its pipe.
CWait == /\ (snap[cur] < gen[cur] \/ proc[cur] = "exited") /\ L("wait", IF cur = 1 THEN "join" ELSE "wait", "reaped")
         /\ cur' = 1 /\ ctl' = (IF FIXED THEN ctl ELSE [ctl EXCEPT ![<<cur, snap[cur]>>] = @ + 1])
         /\ Keep(<<stopReq, stopping, started, dead, snap, gen, proc, said, term, wpc, fails, firstReady, rdy>>)
CKill == /\ KILL /\ lpc = "wait" /\ ~dead /\ snap[cur] = gen[cur] /\ Live(cur) /\ Child(cur, "exited", "kill")
         /\ Keep(<<said, firstReady>>)
\* C:106 / F:159 join every watcher; F:161 closes the ready pipe of each slot of the snapshot.
Join == /\ \A i \in Idx : wpc[i] \in {"none", "done"} /\ L("join", "closed", "joined")
        /\ rdy' = (IF FIXED THEN [s \in Slots |-> IF s[2] = snap[s[1]] THEN rdy[s] + 1 ELSE rdy[s]] ELSE rdy)
        /\ Keep(<<stopReq, stopping, started, dead, cur, snap, gen, proc, said, term, wpc, fails, firstReady, ctl>>)
\* The environment: a stop of the daemon (SIGTERM), and its death (SIGKILL), which closes every pipe end it holds.
StopReq == /\ lpc = "serving" /\ ~stopReq /\ stopReq' = TRUE /\ op' = "stop_req" /\ arg' = 0
           /\ Keep(<<lpc, stopping, started, dead, cur, snap, gen, proc, said, term, wpc, fails, firstReady, ctl, rdy>>)
Die == /\ lpc \notin {"init", "closed"} /\ ~dead /\ dead' = TRUE /\ lpc' = "dead" /\ op' = "die" /\ arg' = 0
       /\ Keep(<<stopReq, stopping, started, cur, snap, gen, proc, said, term, wpc, fails, firstReady, ctl, rdy>>)
Done == /\ lpc \in {"closed", "dead"} /\ \A i \in Idx : (~Live(i) \/ proc[i] = "hung")
        /\ UNCHANGED vars
Leader == \/ Start \/ Await \/ (stopReq /\ Stop("serving", "stop1")) \/ Stop("stop1", "stopped")
          \/ Stop("abort", "stopped") \/ Snap \/ CWait \/ CKill \/ Join
Next == Leader \/ StopReq \/ Die \/ Done \/ \E i \in Idx : Ready(i) \/ Fail(i) \/ Hang(i) \/ End(i) \/ WSteps(i)
\* Fair: the daemon's threads and timeouts, and a child's exit on EOF or SIGTERM. Not fair: a child's start, crash or
\* hang, the stop request and the leader's death: a child may never become ready, and a daemon may serve for ever.
Spec == Init /\ [][Next]_vars /\ WF_vars(Leader) /\ \A i \in Idx : WF_vars(End(i)) /\ WF_vars(WSteps(i))

NoChildOutlivesStop == lpc = "closed" => \A i \in Idx : proc[i] = "exited"
NoDoubleClose == \A s \in Slots : ctl[s] <= 1 /\ rdy[s] <= 1
ClosedOnce == lpc = "closed" => \A i \in Idx, g \in 0..MAXG :
                g <= gen[i] => ctl[<<i, g>>] = 1 /\ (FIXED => rdy[<<i, g>>] = 1)
StartNeedsReady == started => firstReady = Idx
StopEnds == stopReq ~> (dead \/ (lpc = "closed" /\ \A i \in Idx : proc[i] = "exited"))
====
