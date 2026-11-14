---- MODULE flush ----
\* Persistent implementers with a periodic flush to the best. K = piloop/plugins/campaign_type/kit at this commit.
\* Each slot does Periods runs in turn; a run is one period of one slot, and its implementer works until its timeout.
\* While it works it claims any number of open ideas and submits for each claim; at the timeout the daemon cuts it,
\* saves what it left, closes the run and frees the slot, and its claims settle once its submissions are scored. A daemon
\* restart may fail a working run instead, whose claims wait for its submissions' scores the same way. The
\* next run of the slot starts at the best, or moves there at its first claim. A score is "better" or "not": a better
\* first score is evaluated again, and a confirmed one is the new best. TLC checks it
\* (`tlc -config flush_fixed.cfg flush.tla`); tests/daemon/test_flush_model.py replays its graph against the code.
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Slots,        \* the number of slots: the role's max_concurrent
          Periods,      \* the runs of each slot, one after the other
          Ideas,        \* the ideas on the board at the start
          MaxRuns,      \* [stop] max_runs: the runs that reached the evaluator
          MANY,         \* a run may claim any number of ideas; else one (K/backlog/ops.py before run_claims)
          BY_SUBMISSIONS, \* a claim settles by its own submissions; else by the run's end (K/runs/stop.py release)
          TIMEOUT_FINISHES, \* a run that times out after it submitted ends finished (K/runs/controller.py run_outcome)
          SETTLE_WAITS, \* a claim settles only once its run's submissions are scored (K/runs/controller.py _settle)
          SUBMIT_NEEDS_CLAIM, \* a submission is for a claim of its run (K/evaluation/submissions.py refuse)
          BASE_MOVES,   \* a claim before the run's first submission moves it to the best (K/backlog/ops.py _moved)
          STOPS,        \* the campaign may stop while runs work
          RESTARTS,     \* the daemon may stop, or crash, and start again while runs work
          RESTART_WAITS \* a run that a restart fails keeps its claims while a submission waits (optimization/planner.py _fail_run)

Runs == 1..(Slots * Periods)    \* run r follows run r - Slots on its slot
Working == {"running", "wrapping"}
Pending == {"waiting", "better", "confirmed"}
Scored == {"not", "best"}

\* best counts the new bests. base is the best that a run was queued at, read by its claims before its first
\* submission; nothing reads it after that, so a submission sets it to 0, which only merges equal states. atBest says
\* whether the run started from the best as of its last claim before its first submission.
VARIABLES idea, sub, scored, phase, claimed, held, submitted, base, atBest, ended, cut, best, stopped, op, arg
vars == <<idea, sub, scored, phase, claimed, held, submitted, base, atBest, ended, cut, best, stopped, op, arg>>

\* The runs that hold a place under max_runs (K/runs/stop.py:147 runs): the attempts that reached the evaluator, and the
\* open or unsettled ones. A run that the stop cut, or a restart failed, is no attempt.
Attempt(r) == ended[r] \notin {"stopped", "restarted"}
Counted(r) == Attempt(r) /\ submitted[r]
Holding == {r \in Runs : Counted(r) \/ phase[r] \in Working \cup {"cut"} \/ (Attempt(r) /\ held[r] # {})}
Free(r) == IF r <= Slots THEN TRUE ELSE phase[r - Slots] = "closed"

Init == /\ idea = [i \in Ideas |-> "open"] /\ sub = [i \in Ideas |-> "none"] /\ scored = [i \in Ideas |-> FALSE]
        /\ phase = [r \in Runs |-> "none"] /\ claimed = [r \in Runs |-> FALSE] /\ held = [r \in Runs |-> {}]
        /\ submitted = [r \in Runs |-> FALSE] /\ base = [r \in Runs |-> 0] /\ atBest = [r \in Runs |-> TRUE]
        /\ ended = [r \in Runs |-> "none"] /\ cut = [r \in Runs |-> FALSE] /\ best = 0 /\ stopped = FALSE
        /\ op = "init" /\ arg = <<0, 0>>

\* piloop/plugins/campaign_type/optimization/planner.py:208 queue: a run for the free slot, at the type's base, within
\* the runs that max_runs leaves. Its implementer starts at once.
Queue(r) == /\ phase[r] = "none" /\ ~stopped /\ Free(r) /\ Cardinality(Holding) < MaxRuns
            /\ phase' = [phase EXCEPT ![r] = "running"] /\ base' = [base EXCEPT ![r] = best]
            /\ op' = "queue" /\ arg' = <<r, 0>>
            /\ UNCHANGED <<idea, sub, scored, claimed, held, submitted, atBest, ended, cut, best, stopped>>

\* K/backlog/ops.py:106 claim, one write transaction: an open idea, any number for each run (one without MANY). A
\* claim before the run's first submission moves it to the base, which may have moved since its queue (_moved).
Claim(r, i) == /\ phase[r] \in Working /\ idea[i] = "open" /\ (MANY \/ ~claimed[r])
               /\ idea' = [idea EXCEPT ![i] = "claimed"] /\ sub' = [sub EXCEPT ![i] = "none"]
               /\ claimed' = [claimed EXCEPT ![r] = TRUE] /\ held' = [held EXCEPT ![r] = @ \cup {i}]
               /\ IF submitted[r] THEN UNCHANGED <<base, atBest>>
                  ELSE /\ base' = IF BASE_MOVES THEN [base EXCEPT ![r] = best] ELSE base
                       /\ atBest' = [atBest EXCEPT ![r] = BASE_MOVES \/ base[r] = best]
               /\ op' = "claim" /\ arg' = <<r, i>>
               /\ UNCHANGED <<scored, phase, submitted, ended, cut, best, stopped>>

\* K/evaluation/submissions.py:169 submit (`piloop submit --idea`): a version for one of the run's claims, at most one
\* here for each claim. Without SUBMIT_NEEDS_CLAIM, for any idea that waits for no score.
Submit(r, i) == /\ phase[r] \in Working /\ sub[i] = "none" /\ idea[i] # "done"
                /\ IF SUBMIT_NEEDS_CLAIM THEN i \in held[r] ELSE TRUE
                /\ sub' = [sub EXCEPT ![i] = "waiting"] /\ submitted' = [submitted EXCEPT ![r] = TRUE]
                /\ base' = [base EXCEPT ![r] = 0]
                /\ op' = "submit" /\ arg' = <<r, i>>
                /\ UNCHANGED <<idea, scored, phase, claimed, held, atBest, ended, cut, best, stopped>>

\* K/runs/controller.py:904 _score_one, on a thread of its own while the worker works on: the first evaluation. A
\* score that is not better is recorded at once (record_scored); a better one waits for its confirmation. The stop rule
\* holds the scoring of a stopped campaign (optimization/planner.py:189 hold_if_exhausted).
Score(i) == /\ sub[i] = "waiting" /\ ~stopped
            /\ \/ sub' = [sub EXCEPT ![i] = "not"] /\ scored' = [scored EXCEPT ![i] = TRUE]
               \/ sub' = [sub EXCEPT ![i] = "better"] /\ UNCHANGED scored
            /\ op' = "score" /\ arg' = <<0, i>>
            /\ UNCHANGED <<idea, phase, claimed, held, submitted, base, atBest, ended, cut, best, stopped>>

\* K/runs/controller.py:904 _score_one: a mean that beats the best's is evaluated again, until it is confirmed or not.
Confirm(i) == /\ sub[i] = "better" /\ ~stopped
              /\ \/ sub' = [sub EXCEPT ![i] = "confirmed"] /\ UNCHANGED scored
                 \/ sub' = [sub EXCEPT ![i] = "not"] /\ scored' = [scored EXCEPT ![i] = TRUE]
              /\ op' = "confirm" /\ arg' = <<0, i>>
              /\ UNCHANGED <<idea, phase, claimed, held, submitted, base, atBest, ended, cut, best, stopped>>

\* K/runs/controller.py:1005 record_scored of a confirmed candidate: it is the best of the pool now (pool.best_of).
NewBest(i) == /\ sub[i] = "confirmed" /\ ~stopped
              /\ sub' = [sub EXCEPT ![i] = "best"] /\ scored' = [scored EXCEPT ![i] = TRUE] /\ best' = best + 1
              /\ op' = "best" /\ arg' = <<0, i>>
              /\ UNCHANGED <<idea, phase, claimed, held, submitted, base, atBest, ended, cut, stopped>>

\* piloop/plugins/role/consumers.py:433 the wrap-up note, its lead before the timeout. It tells the agent to save; it
\* refuses nothing, so the agent may still claim and submit.
WrapUp(r) == /\ phase[r] = "running"
             /\ phase' = [phase EXCEPT ![r] = "wrapping"] /\ op' = "wrapup" /\ arg' = <<r, 0>>
             /\ UNCHANGED <<idea, sub, scored, claimed, held, submitted, base, atBest, ended, cut, best, stopped>>

\* The runtime kills the implementer at its timeout (piloop/plugins/runtime/subprocess.py:129): the period ends.
Timeout(r) == /\ phase[r] = "wrapping"
              /\ phase' = [phase EXCEPT ![r] = "cut"] /\ op' = "timeout" /\ arg' = <<r, 0>>
              /\ UNCHANGED <<idea, sub, scored, claimed, held, submitted, base, atBest, ended, cut, best, stopped>>

\* K/runs/controller.py:611 _ran: the versions it left are kept as skipped (_unsubmitted), and the run closes as
\* run_outcome says (close_run), which frees its slot. Its submissions are scored after.
Capture(r) == /\ phase[r] = "cut"
              /\ phase' = [phase EXCEPT ![r] = "closed"]
              /\ ended' = [ended EXCEPT ![r] = CASE cut[r] -> "stopped"
                                                [] TIMEOUT_FINISHES /\ submitted[r] -> "finished"
                                                [] OTHER -> "timed_out"]
              /\ op' = "capture" /\ arg' = <<r, 0>>
              /\ UNCHANGED <<idea, sub, scored, claimed, held, submitted, base, atBest, cut, best, stopped>>

\* The ideas once the campaign type settled each claim of run r, in one transaction (K/runs/stop.py:67 settle_claims,
\* :99 release): done once a submission of it scored, else open again. Before run_claims, release made it done only
\* after a finished run.
Settled(r) == [i \in Ideas |-> IF i \notin held[r] THEN idea[i]
                               ELSE IF sub[i] \in Scored /\ (BY_SUBMISSIONS \/ ended[r] = "finished") THEN "done"
                               ELSE "open"]

\* K/runs/controller.py:1219 _settle waits until each submission of the run is scored, then settles its claims.
Settle(r) == /\ phase[r] = "closed" /\ held[r] # {}
             /\ SETTLE_WAITS => \A j \in held[r] : sub[j] \notin Pending
             /\ idea' = Settled(r) /\ held' = [held EXCEPT ![r] = {}]
             /\ op' = "settle" /\ arg' = <<r, 0>>
             /\ UNCHANGED <<sub, scored, phase, claimed, submitted, base, atBest, ended, cut, best, stopped>>

\* The daemon stops, or crashes, and the next one starts without its implementer, which it did not adopt
\* (piloop/plugins/campaign_type/consumers.py:207 stop, :226 start): it fails the run, no attempt
\* (optimization/planner.py:151 _fail_run). Its claims settle now, unless a submission waits: the next daemon scores it
\* and settles them (planner.py:134 resume_unscored, K/runs/controller.py:760 resume, then Settle). Without
\* RESTART_WAITS they settle now.
Restart(r) == /\ RESTARTS /\ phase[r] \in Working \cup {"cut"}
              /\ phase' = [phase EXCEPT ![r] = "closed"] /\ ended' = [ended EXCEPT ![r] = "restarted"]
              /\ IF RESTART_WAITS /\ \E i \in held[r] : sub[i] \in Pending
                 THEN UNCHANGED <<idea, held>>
                 ELSE idea' = Settled(r) /\ held' = [held EXCEPT ![r] = {}]
              /\ op' = "restart" /\ arg' = <<r, 0>>
              /\ UNCHANGED <<sub, scored, claimed, submitted, base, atBest, cut, best, stopped>>

\* The campaign's stop (max_hours, or by hand) cuts every working implementer short (stop_cut) and starts no run.
Stop == /\ STOPS /\ ~stopped /\ stopped' = TRUE
        /\ phase' = [r \in Runs |-> IF phase[r] \in Working THEN "cut" ELSE phase[r]]
        /\ cut' = [r \in Runs |-> cut[r] \/ phase[r] \in Working]
        /\ op' = "stop" /\ arg' = <<0, 0>>
        /\ UNCHANGED <<idea, sub, scored, claimed, held, submitted, base, atBest, ended, best>>

\* The end: no run is open, and every claim settled, but those whose submissions the stop left unscored.
Done == /\ \A r \in Runs : phase[r] \in {"none", "closed"}
        /\ \A r \in Runs : phase[r] = "none" => (stopped \/ ~Free(r) \/ Cardinality(Holding) >= MaxRuns)
        /\ \A r \in Runs : \A i \in held[r] : stopped /\ \E j \in held[r] : sub[j] \in Pending
        /\ UNCHANGED vars

Next == \/ \E r \in Runs : Queue(r) \/ WrapUp(r) \/ Timeout(r) \/ Capture(r) \/ Settle(r) \/ Restart(r)
        \/ \E r \in Runs, i \in Ideas : Claim(r, i) \/ Submit(r, i)
        \/ \E i \in Ideas : Score(i) \/ Confirm(i) \/ NewBest(i)
        \/ Stop \/ Done

\* Assumed: every implementer gets its note and reaches its timeout, the daemon closes its run and settles its claims,
\* and the scoring of a campaign that runs goes on. An agent may never claim or submit, and the stop and a restart may
\* come at any time or never.
Fair == /\ \A r \in Runs : WF_vars(WrapUp(r)) /\ WF_vars(Timeout(r)) /\ WF_vars(Capture(r)) /\ WF_vars(Settle(r))
        /\ \A i \in Ideas : WF_vars(Confirm(i)) /\ WF_vars(NewBest(i))
Spec == Init /\ [][Next]_vars /\ Fair /\ \A i \in Ideas : WF_vars(Score(i))
\* A negative control for ClaimsSettle: the first evaluation of a submission may never run.
UnfairScore == Init /\ [][Next]_vars /\ Fair

\* An idea is open, done, or claimed by exactly the open claim of one run.
NoIdeaLost == \A i \in Ideas : idea[i] = "claimed" <=> \E r \in Runs : i \in held[r]
\* At most one open claim holds an idea, and an idea never opens while a submission for it waits for its score.
NoDoubleRun == \A i \in Ideas : /\ Cardinality({r \in Runs : i \in held[r]}) <= 1
                                /\ sub[i] \in Pending => idea[i] # "open"
\* A submission that waits for its score keeps its claim: the claim settles on its score.
NoSubmissionLost == \A i \in Ideas : sub[i] \in Pending => \E r \in Runs : i \in held[r]
\* An idea whose submission scored does not open again.
ScoredNotRedone == \A i \in Ideas : scored[i] => idea[i] # "open"
\* A run that claimed starts from the best as of its last claim before its first submission.
BaseIsBest == \A r \in Runs : claimed[r] => atBest[r]
\* The attempts that reached the evaluator stay within max_runs.
RunsBound == Cardinality({r \in Runs : Counted(r)}) <= MaxRuns

\* The ideas are interchangeable: a check of the invariants alone may take them up to their order (SYMMETRY).
IdeaSymmetry == Permutations(Ideas)

\* Each period ends, and each claim settles, but one whose submission the stop left unscored.
PeriodsEnd == \A r \in Runs : [](phase[r] \in Working => <>(phase[r] = "closed"))
ClaimsSettle == \A r \in Runs, i \in Ideas : [](i \in held[r] => <>(i \notin held[r] \/ stopped))
SubmissionsScored == \A i \in Ideas : [](sub[i] = "waiting" => <>(sub[i] \in Scored \/ stopped))
====
