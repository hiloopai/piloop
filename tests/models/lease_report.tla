---- MODULE lease_report ----
\* A worker's jobs, its spool and their leases: one worker, two leases. Ported from the Quint model of #767, which
\* lease.tla extends to two workers. Paths are under piloop/plugins/executor/, at 227c975f8 (#820):
\*   Acquire   kit/leases.py:713 Leases.acquire + :863 Leases.started; workers/worker.py:490 Worker._turn
\*   Spool     worker.py:633 Worker._report (the job ends; its thread goes)
\*   Send      worker.py:583 Worker._send from Worker._turn; leases.py:994 Leases.report (core open)
\*   GiveUp    worker.py:613 Worker._rejected: the job's crash report (Report.crashed) takes its place
\*   Refuse    leases.py:994 Leases.report or :863 Leases.started on `Settled`, then :1033 Leases._end_settled
\*   Heartbeat worker.py:490 Worker._turn; leases.py:895 Leases.heartbeat
\*   SelfStop  worker.py:283 Worker._guard_silence
\*   Expire    leases.py:1088 Leases.expire + :1054 Leases._lose
\*   Restart   worker.py:232 Worker.start (register) + :654 Worker._recover; a job it cannot read is lost
\*   Settle    the environment: core closes the job's row elsewhere (a stop, a cancel, rows dropped at start)
\* The server always answers (no partition), so any self-stop is a bug. It may reject any send of a report, for ever.
\* Time is in renew periods; each turn heartbeats first.
EXTENDS Naturals
CONSTANTS FIX_CARRY, FIX_BUDGET, FIX_SETTLED, FIX_GIVEUP, FIX_RECOVER
Ls == {1, 2}
H == 16
TOL == 6        \* toleration (300 s) / renew (10 s), scaled
SELF_STOP == 4  \* toleration - 2 * renew
TRIES == 2      \* report_tries (30), scaled
ENDED == {"reported", "expired", "cancelled"}
\* srv: queued | live | ENDED. wk: idle | running | spooled. proc: the job's process runs. ends: writes that end the
\* lease (each frees its slot). applied: reports core applied. rejects: rejected sends (saturates at TRIES).
VARIABLES t, ls, heard, busyTo, silentStop, spoolExpired, overTried, op
vars == <<t, ls, heard, busyTo, silentStop, spoolExpired, overTried, op>>
Live(l) == l.srv = "live"
Ended(l, st) == [l EXCEPT !.srv = st, !.ends = @ + 1]
Applied(l) == [Ended(l, "reported") EXCEPT !.wk = "idle", !.core = FALSE, !.applied = @ + 1]
Turn == t >= busyTo /\ heard = t   \* the loop is free and has heartbeated
Write(i, l, o) == /\ ls' = [ls EXCEPT ![i] = l] /\ op' = o
                  /\ UNCHANGED <<t, heard, busyTo, silentStop, spoolExpired, overTried>>
Init == /\ t = 0 /\ heard = 0 /\ busyTo = 0 /\ silentStop = FALSE /\ spoolExpired = FALSE /\ overTried = FALSE
        /\ op = "init" /\ ls = [i \in Ls |-> [srv |-> "queued", exp |-> 0, core |-> TRUE, wk |-> "idle", proc |-> FALSE,
                                              ends |-> 0, applied |-> 0, rejects |-> 0]]
Acquire(i) == Turn /\ ls[i].srv = "queued"
              /\ Write(i, [ls[i] EXCEPT !.srv = "live", !.exp = t + TOL, !.wk = "running", !.proc = TRUE], "acquire")
Spool(i) == ls[i].wk = "running" /\ Write(i, [ls[i] EXCEPT !.wk = "spooled", !.proc = FALSE], "spool")
\* Rejected: without the budget, Remote._post resends it for self_stop. At the horizon H the loop is free again: time
\* stops there, and a loop held past it would hold every send for ever.
Min(a, b) == IF a < b THEN a ELSE b
Report(i, ok) == LET l == [ls[i] EXCEPT !.rejects = IF @ < TRIES THEN @ + 1 ELSE TRIES] IN
    /\ Turn /\ ls[i].wk = "spooled" /\ ls[i].core /\ (~FIX_GIVEUP \/ ls[i].rejects < TRIES)
    /\ IF ~ok THEN /\ ls' = [ls EXCEPT ![i] = l] /\ busyTo' = Min(H, t + IF FIX_BUDGET THEN 1 ELSE SELF_STOP + 1)
                   /\ overTried' = (overTried \/ ls[i].rejects >= TRIES) /\ op' = "report_rejected"
                   /\ UNCHANGED <<t, heard, silentStop, spoolExpired>>
              ELSE Live(ls[i]) /\ Write(i, Applied(ls[i]), "report")
Send(i) == Report(i, TRUE) \/ Report(i, FALSE)
GiveUp(i) == FIX_GIVEUP /\ Turn /\ ls[i].wk = "spooled" /\ ls[i].rejects >= TRIES /\ Live(ls[i])
             /\ Write(i, Applied(ls[i]), "give_up")
\* The worker stops the job and forgets it either way.
Refuse(i) == LET dropped == [ls[i] EXCEPT !.wk = "idle", !.proc = FALSE] IN
    Turn /\ ls[i].wk # "idle" /\ ~ls[i].core /\ Live(ls[i])
    /\ Write(i, IF FIX_SETTLED THEN Ended(dropped, "cancelled") ELSE dropped, "refuse")
Heartbeat == /\ t >= busyTo /\ (heard < t \/ t = 0) /\ heard' = t /\ op' = "heartbeat"
             /\ ls' = [i \in Ls |-> IF Live(ls[i]) /\ (ls[i].wk = "running" \/ (FIX_CARRY /\ ls[i].wk = "spooled"))
                                     THEN [ls[i] EXCEPT !.exp = t + TOL] ELSE ls[i]]
             /\ UNCHANGED <<t, busyTo, silentStop, spoolExpired, overTried>>
SelfStop == /\ t - heard > SELF_STOP /\ \E i \in Ls : ls[i].wk = "running"
            /\ ls' = [i \in Ls |-> IF ls[i].wk # "running" THEN ls[i]
                                   ELSE [ls[i] EXCEPT !.wk = "spooled", !.proc = FALSE]]
            /\ silentStop' = TRUE /\ op' = "self_stop" /\ UNCHANGED <<t, heard, busyTo, spoolExpired, overTried>>
Expire(i) == /\ Live(ls[i]) /\ ls[i].exp <= t /\ spoolExpired' = (spoolExpired \/ ls[i].wk = "spooled")
             /\ ls' = [ls EXCEPT ![i] = [Ended(@, "expired") EXCEPT !.core = FALSE]] /\ op' = "expire"
             /\ UNCHANGED <<t, heard, busyTo, silentStop, overTried>>
\* Threads go and the spool stays. A running job that it reads again goes on; one that it cannot read is lost.
Lost(l) == [l EXCEPT !.wk = "idle", !.core = FALSE, !.proc = l.proc /\ ~FIX_RECOVER]
Restart(unreadable) == /\ \E i \in Ls : ls[i].wk # "idle"
    /\ ls' = [i \in Ls |-> IF ls[i].wk # "running" \/ ~unreadable THEN ls[i]
                           ELSE IF Live(ls[i]) THEN Lost(Ended(ls[i], "expired")) ELSE Lost(ls[i])]
    /\ busyTo' = t /\ op' = "restart" /\ UNCHANGED <<t, heard, silentStop, spoolExpired, overTried>>
Settle(i) == ls[i].core /\ ls[i].srv # "queued" /\ Write(i, [ls[i] EXCEPT !.core = FALSE], "settle")
\* Time moves only once the free loop has heartbeated: the loop turns every renew period.
Tick == /\ t < H /\ (t < busyTo \/ heard = t) /\ t' = t + 1 /\ op' = "tick"
        /\ UNCHANGED <<ls, heard, busyTo, silentStop, spoolExpired, overTried>>
\* The end: time ran out (the horizon), or every lease ended and the worker holds nothing.
Done == (t = H \/ \A i \in Ls : ls[i].srv \in ENDED /\ ls[i].wk = "idle") /\ UNCHANGED vars
Next == \/ \E i \in Ls : Acquire(i) \/ Spool(i) \/ Send(i) \/ GiveUp(i) \/ Refuse(i) \/ Expire(i) \/ Settle(i)
        \/ \E u \in BOOLEAN : Restart(u)
        \/ Heartbeat \/ SelfStop \/ Tick \/ Done
\* Assumed: the worker's loop runs (heartbeats, sends, gives up, drops a settled job) and time passes; the server may
\* reject every send. The Quint model's fairness left out Refuse, and then a settled job's spooled lease lives for ever.
Spec == Init /\ [][Next]_vars /\ WF_vars(Heartbeat) /\ WF_vars(Tick)
        /\ \A i \in Ls : WF_vars(GiveUp(i)) /\ WF_vars(Send(i)) /\ WF_vars(Refuse(i))
Unfair == Init /\ [][Next]_vars /\ WF_vars(Heartbeat) /\ WF_vars(Tick)   \* the loop may stop sending: Settles must fail

\* A live lease always has a holder that renews or reports it.
NoOrphan == \A i \in Ls : Live(ls[i]) => ls[i].wk # "idle"
\* A job's process runs only on a live lease that its worker runs: nothing runs that nothing tracks.
NoOrphanProcess == \A i \in Ls : ls[i].proc => (Live(ls[i]) /\ ls[i].wk = "running")
NoSpoolExpiry == ~spoolExpired   \* a lease whose report sits in a heartbeating worker's spool never expires
NoSilentStop == ~silentStop      \* a worker that the server answers never stops its own jobs
FreedOnce == \A i \in Ls : ls[i].ends <= 1 /\ (ls[i].srv \in ENDED <=> ls[i].ends = 1)   \* a slot is freed once
ReportedOnce == \A i \in Ls : ls[i].applied <= 1
TriesBounded == ~overTried       \* no report is sent again after TRIES rejections: the bound liveness rests on
\* A spooled report's lease ends, even if the server rejects every report.
Settles == \A i \in Ls : [](ls[i].wk = "spooled" => <>~Live(ls[i]))
====
