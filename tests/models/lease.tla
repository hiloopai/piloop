---- MODULE lease ----
\* One job's leases between two workers; lease 2 is the job queued again once lease 1 ended. L = kit/leases.py and
\* W = workers/worker.py in piloop/plugins/executor/ at 227c975f8 (#820). Time is in renew periods. TLC checks it
\* (`tlc -config lease_current.cfg lease.tla`); tests/test_lease_model.py replays its graph against the code.
EXTENDS Integers, FiniteSets
CONSTANTS TOL, SLACK, FIX_REGISTER, FIX_RESEND, RECHECK, FENCE, RID, DUE
Ws == {"w1", "w2"}
Ls == {1, 2}
STOP == TOL - 2   \* Timing.self_stop: toleration - 2 * renew
VARIABLES srv, holder, left, run, started, spool, age, down, asking, took, snap, crashed, bad, op, arg
vars == <<srv, holder, left, run, started, spool, age, down, asking, took, snap, crashed, bad, op, arg>>
Live(l) == srv[l] = "live"
Valid(l, w) == Live(l) /\ holder[l] = w /\ (l = 2 \/ srv[2] = "none")   \* L:834 _live: the fence
Queued == {l \in Ls : srv[l] = "queued"}
Held(w) == spool[w] \cup (IF run[w] = 0 THEN {} ELSE {run[w]})
Flag(b, name) == bad' = (IF b THEN bad \cup {name} ELSE bad)
Did(o, w, l) == op' = o /\ arg' = <<w, l>>
Init == /\ srv = [l \in Ls |-> IF l = 1 THEN "queued" ELSE "none"] /\ holder = [l \in Ls |-> "-"]
        /\ left = [l \in Ls |-> 0] /\ run = [w \in Ws |-> 0] /\ started = [w \in Ws |-> FALSE]
        /\ spool = [w \in Ws |-> {}] /\ age = [w \in Ws |-> 0] /\ down = [w \in Ws |-> FALSE]
        /\ asking = [w \in Ws |-> FALSE] /\ took = [w \in Ws |-> 0] /\ snap = [w \in Ws |-> 0]
        /\ crashed = FALSE /\ bad = {} /\ op = "init" /\ arg = <<"-", 0>>
\* W:520 an unanswered acquire keeps its request id. ok: the answer came back; took: what the id holds now.
Answer(w, l, ok, tk) == /\ run' = [run EXCEPT ![w] = IF ok THEN l ELSE 0] /\ started' = [started EXCEPT ![w] = FALSE]
                        /\ asking' = [asking EXCEPT ![w] = ~ok] /\ took' = [took EXCEPT ![w] = IF ok THEN 0 ELSE tk]
\* L:729 with a slack the acquire reads first: the lease of its request id, or nothing, answers from the read (L:735).
\* FIX_RESEND: a resend whose live lease is due a renewal goes to the write, which renews it as a heartbeat does; a
\* grant, too, writes the expiry TOL + SLACK ahead.
AcqRead(w, ok) == LET own == RID /\ took[w] # 0
                      renew == FIX_RESEND /\ own /\ Valid(took[w], w) /\ left[took[w]] < TOL
                      read == SLACK > 0 /\ ~renew /\ (own \/ Queued = {}) IN
    /\ ~down[w] /\ run[w] = 0 /\ snap[w] = 0 /\ (asking[w] \/ Queued # {}) /\ (ok \/ read)
    /\ IF read
       THEN Answer(w, IF own THEN took[w] ELSE 0, ok, took[w]) /\ UNCHANGED snap
       ELSE /\ snap' = [snap EXCEPT ![w] = IF Queued = {} THEN -1 ELSE CHOOSE q \in Queued : TRUE]
            /\ asking' = [asking EXCEPT ![w] = TRUE] /\ UNCHANGED <<run, started, took>>
    /\ Did(IF ok THEN "acq_read" ELSE "acq_read_lost", w, 0)
    /\ UNCHANGED <<srv, holder, left, spool, age, down, crashed, bad>>
\* L:738 under the write lock it reads again (RECHECK); L:744 a queued lease is granted, expiring TOL ahead.
AcqWrite(w, ok) == LET own == RID /\ took[w] # 0
                       l == IF own THEN took[w] ELSE IF ~RECHECK THEN (IF snap[w] > 0 THEN snap[w] ELSE 0)
                            ELSE IF Queued = {} THEN 0 ELSE CHOOSE q \in Queued : TRUE
                       grant == ~own /\ l # 0 IN
    /\ ~down[w] /\ snap[w] # 0 /\ snap' = [snap EXCEPT ![w] = 0]
    /\ srv' = (IF grant THEN [srv EXCEPT ![l] = "live"] ELSE srv)
    /\ holder' = (IF grant THEN [holder EXCEPT ![l] = w] ELSE holder)
    /\ left' = (IF grant THEN [left EXCEPT ![l] = IF FIX_RESEND THEN TOL + SLACK ELSE TOL]
                ELSE IF FIX_RESEND /\ own /\ Valid(l, w) /\ left[l] < TOL THEN [left EXCEPT ![l] = TOL + SLACK]
                ELSE left)
    /\ Flag(grant /\ took[w] # 0, "second_lease") /\ Answer(w, l, ok, IF grant THEN l ELSE took[w])
    /\ Did(IF ok THEN "acq_write" ELSE "acq_write_lost", w, l) /\ UNCHANGED <<spool, age, down, crashed>>
\* L:863 started: the fence refuses a lease that is not live and this worker's, and the job stops.
Start(w) == /\ ~down[w] /\ run[w] # 0 /\ ~started[w]
            /\ IF Valid(run[w], w) \/ ~FENCE THEN started' = [started EXCEPT ![w] = TRUE] /\ UNCHANGED run
               ELSE run' = [run EXCEPT ![w] = 0] /\ UNCHANGED started
            /\ Flag(~Valid(run[w], w) /\ ~FENCE, "stale_applied") /\ Did("start", w, run[w])
            /\ UNCHANGED <<srv, holder, left, spool, age, down, asking, took, snap, crashed>>
\* W:633 the job ends and its report goes to the spool; W:290 a silent worker stops its jobs and spools them too.
Spool(w, o) == /\ spool' = [spool EXCEPT ![w] = @ \cup {run[w]}] /\ run' = [run EXCEPT ![w] = 0]
               /\ started' = [started EXCEPT ![w] = FALSE] /\ Did(o, w, run[w])
               /\ UNCHANGED <<srv, holder, left, age, down, asking, took, snap, crashed, bad>>
Finish(w) == ~down[w] /\ started[w] /\ Spool(w, "finish")
SelfStop(w) == ~down[w] /\ run[w] # 0 /\ age[w] >= STOP /\ Spool(w, "self_stop")
\* L:994 report under the fence (W:583 _send); a refused one is dropped from the spool.
Report(w, l) == /\ ~down[w] /\ l \in spool[w] /\ spool' = [spool EXCEPT ![w] = @ \ {l}]
                /\ srv' = (IF Valid(l, w) \/ ~FENCE THEN [srv EXCEPT ![l] = "reported"] ELSE srv)
                /\ Flag(~Valid(l, w) /\ ~FENCE, "stale_applied") /\ Did("report", w, l)
                /\ UNCHANGED <<holder, left, run, started, age, down, asking, took, snap, crashed>>
\* L:895 heartbeat of every held lease (W:497). L:912 with a slack it writes only when due: forced is a seen_at older
\* than the slack or a new pressure. L:922 a write renews TOL + SLACK ahead. W:503/510 an answer resets the silence
\* and stops a job whose lease is stale.
HB(w, ok, forced) == LET V == {l \in Held(w) : Valid(l, w)}
                         due == SLACK = 0 \/ forced \/ (DUE /\ \E l \in V : left[l] < TOL) IN
    /\ ~down[w] /\ (Held(w) # {} \/ age[w] > 0) /\ (forced => SLACK > 0)
    /\ left' = [l \in Ls |-> IF due /\ l \in V THEN TOL + SLACK ELSE left[l]]
    /\ age' = (IF ok THEN [age EXCEPT ![w] = 0] ELSE age)
    /\ IF ok /\ run[w] # 0 /\ run[w] \notin V
       THEN run' = [run EXCEPT ![w] = 0] /\ started' = [started EXCEPT ![w] = FALSE] ELSE UNCHANGED <<run, started>>
    /\ Did(IF ok THEN "heartbeat" ELSE "heartbeat_lost", w, IF due THEN 1 ELSE 0)
    /\ UNCHANGED <<srv, holder, spool, down, asking, took, snap, crashed, bad>>
\* L:1088 expire a live lease past its expiry; L:1182 core submits the lost job again: a new lease, a new fence.
Expire(l) == /\ Live(l) /\ left[l] = 0 /\ srv' = [srv EXCEPT ![l] = "expired"] /\ Did("expire", "-", l)
             /\ Flag(\E w \in Ws : ~down[w] /\ run[w] = l, "expired_under_job")
             /\ UNCHANGED <<holder, left, run, started, spool, age, down, asking, took, snap, crashed>>
Requeue == /\ srv[1] = "expired" /\ srv[2] = "none" /\ srv' = [srv EXCEPT ![2] = "queued"] /\ Did("requeue", "-", 2)
           /\ UNCHANGED <<holder, left, run, started, spool, age, down, asking, took, snap, crashed, bad>>
\* W:290 the silence guard keeps its own clock: time does not pass a due self-stop.
Tick == LET left2 == [l \in Ls |-> IF Live(l) /\ left[l] > 0 THEN left[l] - 1 ELSE left[l]]
            age2 == [w \in Ws |-> IF ~down[w] /\ age[w] < STOP THEN age[w] + 1 ELSE age[w]] IN
    /\ ~(\E w \in Ws : ~down[w] /\ run[w] # 0 /\ age[w] >= STOP) /\ <<left2, age2>> # <<left, age>>
    /\ left' = left2 /\ age' = age2 /\ Did("tick", "-", 0)
    /\ UNCHANGED <<srv, holder, run, started, spool, down, asking, took, snap, crashed, bad>>
\* One crash of w1, after its job started (an unstarted one is lease_report.tla's Restart). Its job's process runs on.
Crash == /\ ~crashed /\ (run["w1"] = 0 \/ started["w1"]) /\ crashed' = TRUE /\ down' = [down EXCEPT !["w1"] = TRUE]
         /\ asking' = [asking EXCEPT !["w1"] = FALSE] /\ took' = [took EXCEPT !["w1"] = 0]
         /\ snap' = [snap EXCEPT !["w1"] = 0] /\ Did("crash", "w1", 0)
         /\ UNCHANGED <<srv, holder, left, run, started, spool, age, bad>>
\* W:243 start counts the silence from now; L:643 register returns the live leases, which W:271 recovers.
\* FIX_REGISTER: register also renews them.
Restart == LET keep == run["w1"] # 0 /\ Valid(run["w1"], "w1") IN
    /\ down["w1"] /\ down' = [down EXCEPT !["w1"] = FALSE] /\ age' = [age EXCEPT !["w1"] = 0]
    /\ run' = (IF keep THEN run ELSE [run EXCEPT !["w1"] = 0])
    /\ started' = (IF keep THEN started ELSE [started EXCEPT !["w1"] = FALSE])
    /\ left' = [l \in Ls |-> IF FIX_REGISTER /\ Valid(l, "w1") THEN TOL + SLACK ELSE left[l]]
    /\ Did("restart", "w1", 0) /\ UNCHANGED <<srv, holder, spool, asking, took, snap, crashed, bad>>
Done == /\ \A l \in Ls : srv[l] \notin {"queued", "live"} /\ \A w \in Ws : run[w] = 0 /\ spool[w] = {}
        /\ UNCHANGED vars
Worker(w) == \/ \E ok \in BOOLEAN : AcqRead(w, ok) \/ AcqWrite(w, ok) \/ \E f \in BOOLEAN : HB(w, ok, f)
             \/ Start(w) \/ Finish(w) \/ SelfStop(w) \/ \E l \in Ls : Report(w, l)
Next == (\E w \in Ws : Worker(w)) \/ (\E l \in Ls : Expire(l)) \/ Requeue \/ Tick \/ Crash \/ Restart \/ Done
\* Assumed: w2 never crashes; a call of w2 sent again and again is answered at last (strong fairness: its answers
\* can be lost); w2's start, the daemon's expiry, core's requeue and time are fair. w1, the crash and restart are not.
Fair == /\ SF_vars(AcqWrite("w2", TRUE)) /\ SF_vars(HB("w2", TRUE, FALSE)) /\ WF_vars(Start("w2"))
        /\ WF_vars(\E l \in Ls : Expire(l)) /\ WF_vars(Requeue) /\ WF_vars(Tick)
Spec == Init /\ [][Next]_vars /\ Fair /\ SF_vars(AcqRead("w2", TRUE))
UnfairSpec == Init /\ [][Next]_vars /\ Fair   \* without a fair acquire of w2: JobRuns must fail
OneLiveHolder == Cardinality({l \in Ls : Live(l)}) <= 1
                 /\ Cardinality({w \in Ws : ~down[w] /\ run[w] # 0 /\ started[w]}) <= 1
NoExpiryUnderJob == "expired_under_job" \notin bad
StaleRefused == "stale_applied" \notin bad
OneLeasePerRequest == "second_lease" \notin bad
JobRuns == \A l \in Ls : srv[l] = "queued" ~> srv[l] # "queued"
====
