---- MODULE term ----
\* The `bucket` leader's term record (piloop/plugins/leader/bucket.py) and the daemon that holds it
\* (piloop/daemon/holding.py, piloop/daemon/main.py), as on branch feat/leader-seam: two hosts, one S3 object, conditional writes, coarse ticks of real time.
\* Each host's wall clock is real time plus a fixed offset in 0..SKEW; a taker allows MAX_SKEW. A holder's `until` is its
\* own wall clock at the send of the write that took or renewed the term, plus TTL. Ages count ticks since that send.
\* Faults (a pause, a crash, a slow request, a graceful stop) are bounded by FAULTS; between them, a host that is not
\* paused takes its due steps within the tick (`Due`). TLC: `-config term_fixed.cfg term.tla`.
\* tests/daemon/test_term_model.py replays its graph against the real provider on moto.
EXTENDS Integers, FiniteSets
CONSTANTS TTL, RENEW, SKEW, MAX_SKEW, FAULTS,
          FROM_SEND,   \* the holder's deadline counts from its write's send (TRUE) or from the answer (FALSE)
          RELEASE,     \* a release writes `until = now` ("expire") or deletes the object ("delete")
          FENCE,       \* a holder's writes go where only its own epoch reads them (L5b: the replica under terms/<epoch>/)
          EXIT         \* at its deadline the holder exits at once ("hard") or stops as on SIGTERM ("graceful")
VARIABLES phase, paused, left, ep, seen, req, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused, op, arg
vars == <<phase, paused, left, ep, seen, req, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused, op, arg>>
Hosts == {1, 2}
CAP == TTL + MAX_SKEW + SKEW + 1     \* an age past every expiry: older ones act the same
Sent == {"take", "renew", "release"}
Answered == {"took", "renewed", "released", "refused"}
Offsets == [Hosts -> 0..SKEW]
Skewed == {<<0, SKEW>>}   \* `Offsets <- Skewed`: one start, host 2's clock ahead (the hosts take turns holding)
Max(a, b) == IF a > b THEN a ELSE b
Inc(n) == IF n < CAP THEN n + 1 ELSE CAP
Others(h, f, v) == [g \in Hosts |-> IF g = h THEN f[g] ELSE v]

Init == /\ phase = [h \in Hosts |-> "standby"] /\ paused = [h \in Hosts |-> FALSE] /\ left = [h \in Hosts |-> 0]
        /\ ep = [h \in Hosts |-> 0] /\ seen = [h \in Hosts |-> FALSE] /\ req = [h \in Hosts |-> "none"]
        /\ rage = [h \in Hosts |-> 0] /\ rok = [h \in Hosts |-> FALSE] /\ create = [h \in Hosts |-> FALSE]
        /\ slow = [h \in Hosts |-> FALSE] /\ gated = [h \in Hosts |-> FALSE]
        /\ rec = [none |-> TRUE, epoch |-> 0, holder |-> 1, age |-> 0, off |-> 0, ttl |-> TTL]
        /\ off \in Offsets /\ top = 0 /\ faults = 0 /\ mixed = FALSE /\ reused = FALSE
        /\ op = "init" /\ arg = 0

\* bucket.py:149 `_expired`: the reader's wall clock has reached the record's `until` plus MAX_SKEW.
Expired(h) == rec.age >= rec.ttl + MAX_SKEW + rec.off - off[h]
Writing(h) == (phase[h] = "holding" /\ left[h] > 0) \/ phase[h] = "stopping"
Up(h) == phase[h] # "down" /\ ~paused[h]
Fault == faults < FAULTS /\ faults' = faults + 1
Keep(v) == UNCHANGED v

\* bucket.py:157 `_read`: GET the record. A host acts on what it read; once anyone writes, it must read again (`seen`), since
\* an If-Match on an older ETag fails: the body always changes (a new epoch or a later `until`), so an ETag never recurs.
Read(h) == /\ phase[h] = "standby" /\ Up(h) /\ req[h] = "none" /\ ~seen[h]
           /\ seen' = [seen EXCEPT ![h] = TRUE] /\ op' = "read" /\ arg' = h
           /\ Keep(<<phase, paused, left, ep, req, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused>>)
\* bucket.py:117 `attempt`: no record, PUT If-None-Match: *; an expired one, PUT If-Match: its ETag, with the next epoch.
Take(h) == /\ phase[h] = "standby" /\ Up(h) /\ req[h] = "none" /\ seen[h] /\ (rec.none \/ Expired(h))
           /\ req' = [req EXCEPT ![h] = "take"] /\ rage' = [rage EXCEPT ![h] = 0] /\ rok' = [rok EXCEPT ![h] = TRUE]
           /\ create' = [create EXCEPT ![h] = rec.none] /\ slow' = [slow EXCEPT ![h] = FALSE]
           /\ op' = "take" /\ arg' = h
           /\ Keep(<<phase, paused, left, ep, seen, gated, rec, off, top, faults, mixed, reused>>)
\* holding.py:88 `_renewing` -> bucket.py:127 `renew`: PUT If-Match: the ETag of the holder's last write, `until = now + TTL`.
Renew(h) == /\ phase[h] = "holding" /\ Up(h) /\ req[h] = "none" /\ left[h] > 0 /\ left[h] <= TTL - RENEW
            /\ req' = [req EXCEPT ![h] = "renew"] /\ rage' = [rage EXCEPT ![h] = 0] /\ rok' = [rok EXCEPT ![h] = seen[h]]
            /\ create' = [create EXCEPT ![h] = FALSE] /\ slow' = [slow EXCEPT ![h] = FALSE]
            /\ op' = "renew" /\ arg' = h
            /\ Keep(<<phase, paused, left, ep, seen, gated, rec, off, top, faults, mixed, reused>>)
\* main.py:320 `_release_term` -> bucket.py:130 `release`, after the daemon's last write: PUT If-Match, `until = now`. A fault.
Release(h) == /\ phase[h] = "holding" /\ Up(h) /\ req[h] = "none" /\ left[h] > 0 /\ ~gated[h] /\ Fault
              /\ phase' = [phase EXCEPT ![h] = "releasing"] /\ req' = [req EXCEPT ![h] = "release"]
              /\ rage' = [rage EXCEPT ![h] = 0] /\ rok' = [rok EXCEPT ![h] = seen[h]]
              /\ create' = [create EXCEPT ![h] = FALSE] /\ slow' = [slow EXCEPT ![h] = FALSE]
              /\ op' = "release" /\ arg' = h /\ Keep(<<paused, left, ep, seen, gated, rec, off, top, mixed, reused>>)
\* bucket.py:173 `_write`: S3 applies the PUT at once, whatever the host does now: it may be paused, or dead. A dead host's answer is lost.
Land(h) == LET ok == IF create[h] THEN rec.none ELSE rok[h] /\ ~rec.none
               new == IF rec.none THEN 1 ELSE rec.epoch + 1
               took == ok /\ req[h] = "take"
               written == [none |-> FALSE, holder |-> h, age |-> rage[h], off |-> off[h],
                           epoch |-> IF took THEN new ELSE rec.epoch, ttl |-> IF req[h] = "release" THEN 0 ELSE TTL]
           IN /\ req[h] \in Sent
              /\ rec' = IF ~ok THEN rec ELSE IF req[h] = "release" /\ RELEASE = "delete" THEN [rec EXCEPT !.none = TRUE]
                        ELSE written
              /\ seen' = IF ok THEN [g \in Hosts |-> g = h] ELSE seen
              /\ rok' = IF ok THEN [g \in Hosts |-> FALSE] ELSE [rok EXCEPT ![h] = FALSE]
              /\ ep' = IF took THEN [ep EXCEPT ![h] = new] ELSE ep
              /\ top' = (IF took THEN Max(top, new) ELSE top) /\ reused' = (reused \/ (took /\ new <= top))
              /\ req' = [req EXCEPT ![h] = IF phase[h] = "down" THEN "none" ELSE IF ~ok THEN "refused"
                                         ELSE CASE req[h] = "take" -> "took" [] req[h] = "renew" -> "renewed"
                                                [] OTHER -> "released"]
              /\ op' = "land" /\ arg' = h /\ Keep(<<phase, paused, left, rage, create, slow, gated, off, faults, mixed>>)
\* The answer. holding.py:26 `deadline`: the holder writes until `until` by its clock, so TTL less the ticks since the
\* send (FROM_SEND); counted from the answer, it would run past the record's `until`. A refused renew is `LostTerm`:
\* the daemon exits at once, as a refused take sends the host back to read.
Answer(h) == LET from == IF FROM_SEND THEN Max(TTL - rage[h], 0) ELSE TTL
             IN /\ req[h] \in Answered /\ Up(h) /\ req' = [req EXCEPT ![h] = "none"] /\ op' = "answer" /\ arg' = h
                /\ CASE req[h] \in {"took", "renewed"} -> phase' = [phase EXCEPT ![h] = "holding"]
                                                         /\ left' = [left EXCEPT ![h] = from] /\ Keep(seen)
                     [] req[h] = "refused" /\ phase[h] = "standby" -> seen' = [seen EXCEPT ![h] = FALSE]
                                                                    /\ Keep(<<phase, left>>)
                     [] OTHER -> phase' = [phase EXCEPT ![h] = "down"] /\ left' = [left EXCEPT ![h] = 0]
                                 /\ seen' = [seen EXCEPT ![h] = FALSE]
                /\ Keep(<<paused, ep, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused>>)
\* A write of the daemon, in two steps: it checks that its term holds, then commits. A pause can fall in between.
Gate(h) == /\ Writing(h) /\ ~paused[h] /\ ~gated[h] /\ gated' = [gated EXCEPT ![h] = TRUE] /\ op' = "gate" /\ arg' = h
           /\ Keep(<<phase, paused, left, ep, seen, req, rage, rok, create, slow, rec, off, top, faults, mixed, reused>>)
\* A commit of an epoch older than the newest lands where the newer term reads unless FENCE keeps it apart.
Commit(h) == /\ gated[h] /\ ~paused[h] /\ gated' = [gated EXCEPT ![h] = FALSE]
             /\ mixed' = (mixed \/ (~FENCE /\ ep[h] < top)) /\ op' = "commit" /\ arg' = h
             /\ Keep(<<phase, paused, left, ep, seen, req, rage, rok, create, slow, rec, off, top, faults, reused>>)
\* The process ends: an answer that it did not take is lost; a request still on its way may land.
Gone(h) == /\ phase' = [phase EXCEPT ![h] = "down"] /\ gated' = [gated EXCEPT ![h] = FALSE]
           /\ seen' = [seen EXCEPT ![h] = FALSE] /\ req' = [req EXCEPT ![h] = IF @ \in Answered THEN "none" ELSE @]
\* holding.py:82 `_watching`: at its deadline the daemon exits with no more writes (`os._exit`). A graceful stop instead
\* (the scheduler's interrupt, then `shutdown`) goes on writing for as long as it takes: `Exit` ends it.
SelfStop(h) == /\ phase[h] = "holding" /\ ~paused[h] /\ left[h] = 0 /\ op' = "self_stop" /\ arg' = h
               /\ IF EXIT = "hard" THEN Gone(h) ELSE phase' = [phase EXCEPT ![h] = "stopping"] /\ Keep(<<gated, seen, req>>)
               /\ Keep(<<paused, left, ep, rage, rok, create, slow, rec, off, top, faults, mixed, reused>>)
Exit(h) == /\ phase[h] = "stopping" /\ ~paused[h] /\ ~gated[h] /\ Gone(h) /\ op' = "exit" /\ arg' = h
           /\ Keep(<<paused, left, ep, rage, rok, create, slow, rec, off, top, faults, mixed, reused>>)
Crash(h) == /\ phase[h] # "down" /\ Fault /\ Gone(h)
            /\ paused' = [paused EXCEPT ![h] = FALSE] /\ left' = [left EXCEPT ![h] = 0] /\ op' = "crash" /\ arg' = h
            /\ Keep(<<ep, rage, rok, create, slow, rec, off, top, mixed, reused>>)
\* main.py:300 `_take`: a new `piloop serve`, a new process, which never adopts a record that names its host.
Restart(h) == /\ phase[h] = "down" /\ req[h] = "none" /\ phase' = [phase EXCEPT ![h] = "standby"]
              /\ ep' = [ep EXCEPT ![h] = 0] /\ op' = "restart" /\ arg' = h
              /\ Keep(<<paused, left, seen, req, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused>>)
Pause(h) == /\ phase[h] # "down" /\ ~paused[h] /\ Fault /\ paused' = [paused EXCEPT ![h] = TRUE]
            /\ op' = "pause" /\ arg' = h
            /\ Keep(<<phase, left, ep, seen, req, rage, rok, create, slow, gated, rec, off, top, mixed, reused>>)
Resume(h) == /\ paused[h] /\ paused' = [paused EXCEPT ![h] = FALSE] /\ op' = "resume" /\ arg' = h
             /\ Keep(<<phase, left, ep, seen, req, rage, rok, create, slow, gated, rec, off, top, faults, mixed, reused>>)
Slow(h) == /\ req[h] \in Sent /\ ~slow[h] /\ Fault /\ slow' = [slow EXCEPT ![h] = TRUE] /\ op' = "slow" /\ arg' = h
           /\ Keep(<<phase, paused, left, ep, seen, req, rage, rok, create, gated, rec, off, top, mixed, reused>>)
\* What must happen within the tick: a request lands unless slow; a host that is not paused takes its answer, commits,
\* stops at its deadline, and renews when due.
Due(h) == \/ req[h] \in Sent /\ ~slow[h]
          \/ /\ Up(h)
             /\ \/ req[h] \in Answered
                \/ gated[h]
                \/ phase[h] = "holding" /\ left[h] = 0
                \/ phase[h] = "holding" /\ req[h] = "none" /\ left[h] <= TTL - RENEW
Tick == /\ \A h \in Hosts : ~Due(h)
        /\ rec' = [rec EXCEPT !.age = Inc(@)] /\ rage' = [h \in Hosts |-> IF req[h] # "none" THEN Inc(rage[h]) ELSE 0]
        /\ left' = [h \in Hosts |-> Max(left[h] - 1, 0)] /\ op' = "tick" /\ arg' = 0
        /\ Keep(<<phase, paused, ep, seen, req, rok, create, slow, gated, off, top, faults, mixed, reused>>)

\* A graph for the replays leaves out the steps that no code of L5a takes: `Gate <- Never` in its cfg.
Never(h) == FALSE

Next == \/ Tick
        \/ \E h \in Hosts : \/ Read(h) \/ Take(h) \/ Renew(h) \/ Release(h) \/ Land(h) \/ Answer(h) \/ Gate(h)
                            \/ Commit(h) \/ SelfStop(h) \/ Exit(h) \/ Crash(h) \/ Restart(h) \/ Pause(h) \/ Resume(h) \/ Slow(h)
\* Assumed: time passes; a host does its steps, a paused one resumes, a dead one starts again, a request lands.
\* Faults are not fair, and there are at most FAULTS of them.
Steps(h) == WF_vars(Read(h)) /\ WF_vars(Take(h)) /\ WF_vars(Renew(h)) /\ WF_vars(Land(h)) /\ WF_vars(Answer(h))
            /\ WF_vars(Commit(h)) /\ WF_vars(SelfStop(h)) /\ WF_vars(Exit(h)) /\ WF_vars(Restart(h))
            /\ WF_vars(Resume(h))
Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ \A h \in Hosts : Steps(h)
\* No fair take: Elected must fail.
NoTakeSteps(h) == WF_vars(Read(h)) /\ WF_vars(Renew(h)) /\ WF_vars(Land(h)) /\ WF_vars(Answer(h))
                  /\ WF_vars(Commit(h)) /\ WF_vars(SelfStop(h)) /\ WF_vars(Exit(h)) /\ WF_vars(Restart(h))
                  /\ WF_vars(Resume(h))
Unfair == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ \A h \in Hosts : NoTakeSteps(h)

\* At any instant at most one host may write: the lease.
SingleWriter == Cardinality({h \in Hosts : Writing(h)}) <= 1
\* No write of an older epoch lands where a newer term reads, however long a holder was paused: the fence.
NoStaleWrite == ~mixed
\* Every take writes an epoch above every epoch before it.
EpochsIncrease == ~reused /\ (~rec.none => rec.epoch = top)
\* Once faults stop, some host holds the term, and keeps it.
Elected == <>[](\E h \in Hosts : Writing(h))
====
