---- MODULE forward ----
\* L5c, as on branch feat/forward: a `piloop serve` that does not hold the home's term forwards each request to the holder
\* (piloop/net/forward.py), and a standby refuses a request that carries `Piloop-Term` (forward.py:69 `refusal`): a
\* holder ignores the header. Two hosts, REQS requests. The term itself is abstract: tests/models/term.tla shows that no host takes a term while another may still write, which `Take`'s guard
\* (`~rec.live`) stands for.
\* A forwarder reads the record every `renew` (`Refresh`), so its view may be stale. Faults (a crash, a graceful stop)
\* are bounded by FAULTS. TLC: `-config forward_fixed.cfg forward.tla`.
\* tests/net/test_forward_model.py replays its graph against the real forwarder.
EXTENDS Integers
CONSTANTS FAULTS, REQS,
          REFUSE,   \* a server that does not hold the term a request names answers 409 (TRUE), or a standby forwards it again
          CANCEL,   \* a forwarder answers 503 to what it sent to a term, once it reads another or takes one (TRUE)
          RESEND,   \* a forwarder that got no answer sends the request again, to the term that it reads next
          OWN_DB     \* a standby with no term to forward to serves the request from its own database
VARIABLES phase, ep, view, rec, st, at, via, sent, res, ans, hops, applied, bad, faults, op, arg
vars == <<phase, ep, view, rec, st, at, via, sent, res, ans, hops, applied, bad, faults, op, arg>>
Hosts == {1, 2}
Reqs == 1..REQS
MAX_EPOCH == 3
None == [epoch |-> 0, holder |-> 0]
Up(h) == phase[h] # "down"
Fault == faults < FAULTS /\ faults' = faults + 1
Keep(v) == UNCHANGED v
Held == {"in", "app"}   \* a request that a server accepted and has not answered

Init == /\ phase = [h \in Hosts |-> "standby"] /\ ep = [h \in Hosts |-> 0] /\ view = [h \in Hosts |-> None]
        /\ rec = [epoch |-> 0, holder |-> 0, live |-> FALSE]
        /\ st = [r \in Reqs |-> "new"] /\ at = [r \in Reqs |-> 0] /\ via = [r \in Reqs |-> 0]
        /\ sent = [r \in Reqs |-> 0] /\ res = [r \in Reqs |-> "none"] /\ ans = [r \in Reqs |-> "none"]
        /\ hops = [r \in Reqs |-> 0] /\ applied = [r \in Reqs |-> 0] /\ bad = FALSE /\ faults = 0
        /\ op = "init" /\ arg = 0

\* forward.py:212 `_seen`, :192 `_hold`: what a forwarder sent to a term other than `e` answers 503 at once
\* (:256 `_forward`), unless its answer is on the way back.
Abandoned(h, e, r) == CANCEL /\ via[r] = h /\ st[r] \in {"in", "app", "lost"} /\ sent[r] # e
Abandon(h, e) == /\ st' = [r \in Reqs |-> IF Abandoned(h, e, r) THEN "done" ELSE st[r]]
                 /\ ans' = [r \in Reqs |-> IF Abandoned(h, e, r) THEN "503" ELSE ans[r]]
\* The term record (piloop/plugins/leader/bucket.py:242 `attempt`). main.py:381 `_take`: a standby takes the next term
\* once none is live. main.py:177 -> forward.py:136 `hold`: from the take on, its forwarder answers 503, and ends what it
\* had sent.
Take(h) == /\ phase[h] = "standby" /\ ~rec.live /\ rec.epoch < MAX_EPOCH
           /\ rec' = [epoch |-> rec.epoch + 1, holder |-> h, live |-> TRUE]
           /\ phase' = [phase EXCEPT ![h] = "starting"] /\ ep' = [ep EXCEPT ![h] = rec.epoch + 1]
           /\ Abandon(h, -1) /\ op' = "take" /\ arg' = h
           /\ Keep(<<view, at, via, sent, res, hops, applied, bad, faults>>)
\* main.py:346 `_listen`: the restore, the migration and the rebuild are done; the daemon's own servers serve once
\* `Forwarding.close` (forward.py:145) returned, which waits for the forwarder's requests to end.
Serve(h) == /\ phase[h] = "starting" /\ \A r \in Reqs : via[r] = h => st[r] \in {"new", "done"}
            /\ phase' = [phase EXCEPT ![h] = "serving"] /\ op' = "serve" /\ arg' = h
            /\ Keep(<<ep, view, rec, st, at, via, sent, res, ans, hops, applied, bad, faults>>)
\* bucket.py:329 `current`: a record whose holder renews no more ends at its `until`. Its process is gone: dead, or another.
Expire == /\ rec.live /\ phase[rec.holder] \in {"down", "standby"} /\ rec' = [rec EXCEPT !.live = FALSE]
          /\ op' = "expire" /\ arg' = rec.holder
          /\ Keep(<<phase, ep, view, st, at, via, sent, res, ans, hops, applied, bad, faults>>)
\* forward.py:196 `_read`: every `forward_refresh`, a standby reads the term that a daemon holds now (`Leader.current`);
\* a new one ends what went to the one before (:212 `_seen`).
Refresh(h) == LET now == IF rec.live THEN [epoch |-> rec.epoch, holder |-> rec.holder] ELSE None
              IN /\ phase[h] = "standby" /\ view[h] # now /\ view' = [view EXCEPT ![h] = now]
                 /\ Abandon(h, now.epoch) /\ op' = "refresh" /\ arg' = h
                 /\ Keep(<<phase, ep, rec, at, via, sent, res, hops, applied, bad, faults>>)

\* A client sends a request to a host that serves: a load balancer routes to the ready ones (`/v1/ready`).
Send(r, h) == /\ st[r] = "new" /\ Up(h) /\ st' = [st EXCEPT ![r] = "in"] /\ at' = [at EXCEPT ![r] = h]
              /\ op' = "send" /\ arg' = r
              /\ Keep(<<phase, ep, view, rec, via, sent, res, ans, hops, applied, bad, faults>>)
\* What answers a request goes back to the forwarder that sent it, else to the client.
Reply(r, x) == IF via[r] = 0 THEN st' = [st EXCEPT ![r] = "done"] /\ ans' = [ans EXCEPT ![r] = x] /\ Keep(res)
               ELSE st' = [st EXCEPT ![r] = "reply"] /\ res' = [res EXCEPT ![r] = x] /\ Keep(ans)
Apply(r, h) == /\ applied' = [applied EXCEPT ![r] = @ + 1] /\ st' = [st EXCEPT ![r] = "app"]
               /\ bad' = (bad \/ ~(phase[h] = "serving" /\ rec.live /\ rec.holder = h /\ ep[h] = rec.epoch))
               /\ Keep(<<res, ans>>)
\* The server at `at[r]` handles it: the holder serves whatever term the request names (a holder that took a later
\* term at the same address is the holder all the same), or a standby's `__call__`: the refusal, a daemon that
\* starts, no term, else the forward (forward.py `Forwarding.__call__`).
Handle(r) == LET h == at[r] IN
             /\ st[r] = "in" /\ op' = "handle" /\ arg' = r
             /\ CASE ~Up(h) -> Reply(r, "refused") /\ Keep(<<at, via, sent, hops, applied, bad>>)
                  [] phase[h] = "serving" -> Apply(r, h) /\ Keep(<<at, via, sent, hops>>)
                  [] phase[h] = "starting" -> Reply(r, "503") /\ Keep(<<at, via, sent, hops, applied, bad>>)
                  [] sent[r] # 0 /\ REFUSE -> Reply(r, "409") /\ Keep(<<at, via, sent, hops, applied, bad>>)
                  [] view[h].holder # 0 /\ hops[r] < 2 ->
                        /\ at' = [at EXCEPT ![r] = view[h].holder] /\ sent' = [sent EXCEPT ![r] = view[h].epoch]
                        /\ via' = [via EXCEPT ![r] = IF @ = 0 THEN h ELSE @] /\ hops' = [hops EXCEPT ![r] = @ + 1]
                        /\ Keep(<<st, res, ans, applied, bad>>)
                  [] view[h].holder = 0 /\ OWN_DB -> Apply(r, h) /\ Keep(<<at, via, sent, hops>>)
                  [] OTHER -> Reply(r, "503") /\ Keep(<<at, via, sent, hops, applied, bad>>)
             /\ Keep(<<phase, ep, view, rec, faults>>)
\* The holder answers what it applied.
Answer(r) == /\ st[r] = "app" /\ Up(at[r]) /\ Reply(r, "ok") /\ op' = "answer" /\ arg' = r
             /\ Keep(<<phase, ep, view, rec, at, via, sent, hops, applied, bad, faults>>)
\* forward.py:335 `_relay`, :256 `_forward`: the forwarder passes the holder's answer on; any other is 503, Retry-After.
Relay(r) == /\ st[r] = "reply" /\ Up(via[r]) /\ st' = [st EXCEPT ![r] = "done"]
            /\ ans' = [ans EXCEPT ![r] = IF res[r] = "ok" THEN "ok" ELSE "503"] /\ op' = "relay" /\ arg' = r
            /\ Keep(<<phase, ep, view, rec, at, via, sent, res, hops, applied, bad, faults>>)
\* What the design does not do: send a request again once its answer is lost.
Resend(r) == /\ RESEND /\ st[r] = "lost" /\ phase[via[r]] = "standby" /\ view[via[r]].holder # 0
             /\ view[via[r]].epoch # sent[r]
             /\ st' = [st EXCEPT ![r] = "in"] /\ at' = [at EXCEPT ![r] = view[via[r]].holder]
             /\ sent' = [sent EXCEPT ![r] = view[via[r]].epoch] /\ op' = "resend" /\ arg' = r
             /\ Keep(<<phase, ep, view, rec, via, res, ans, hops, applied, bad, faults>>)

\* The process ends with no answer to what it held: a forwarder waits for an answer that never comes ("lost"); the
\* client of a request that it forwarded sees its connection reset.
Ends(h) == /\ st' = [r \in Reqs |-> IF st[r] \in {"new", "done"} THEN st[r]
                                    ELSE IF via[r] = h THEN "done"
                                    ELSE IF at[r] = h /\ st[r] \in Held THEN IF via[r] = 0 THEN "done" ELSE "lost"
                                    ELSE st[r]]
           /\ ans' = [r \in Reqs |-> IF st[r] \notin {"new", "done"} /\ (via[r] = h \/ (at[r] = h /\ st[r] \in Held /\ via[r] = 0))
                                     THEN "reset" ELSE ans[r]]
\* A crash, on a machine that fails: the requests that it held hang (no reset). The record ends at its `until`.
Crash(h) == /\ Up(h) /\ Fault /\ Ends(h) /\ phase' = [phase EXCEPT ![h] = "down"] /\ ep' = [ep EXCEPT ![h] = 0]
            /\ op' = "crash" /\ arg' = h /\ Keep(<<view, rec, at, via, sent, res, hops, applied, bad>>)
\* main.py:439 `shutdown`: the servers let the requests in flight finish, then the term is released (:405).
Stop(h) == /\ phase[h] = "serving" /\ \A r \in Reqs : ~(at[r] = h /\ st[r] \in Held) /\ Fault
           /\ Ends(h) /\ phase' = [phase EXCEPT ![h] = "down"] /\ ep' = [ep EXCEPT ![h] = 0]
           /\ rec' = [rec EXCEPT !.live = FALSE] /\ op' = "stop" /\ arg' = h
           /\ Keep(<<view, at, via, sent, res, hops, applied, bad>>)
\* A new `piloop serve` on the host: a standby that has read no term yet.
Restart(h) == /\ phase[h] = "down" /\ phase' = [phase EXCEPT ![h] = "standby"] /\ view' = [view EXCEPT ![h] = None]
              /\ op' = "restart" /\ arg' = h /\ Keep(<<ep, rec, st, at, via, sent, res, ans, hops, applied, bad, faults>>)
\* Every request answered, and nothing left to happen but a new term past MAX_EPOCH.
Done == /\ \A r \in Reqs : st[r] \in {"new", "done"} /\ op' = "done" /\ arg' = 0 /\ Keep(<<phase, ep, view, rec, st, at,
           via, sent, res, ans, hops, applied, bad, faults>>)

Next == \/ Expire \/ Done
        \/ \E h \in Hosts : Take(h) \/ Serve(h) \/ Refresh(h) \/ Crash(h) \/ Stop(h) \/ Restart(h)
        \/ \E r \in Reqs : Handle(r) \/ Answer(r) \/ Relay(r) \/ Resend(r) \/ \E h \in Hosts : Send(r, h)
\* Assumed: each host does its steps, a dead one starts again, a record whose holder is gone ends. Faults are not fair,
\* and there are at most FAULTS of them. A client's timeout is not modelled: what the forwarder does must answer.
Spec == /\ Init /\ [][Next]_vars /\ WF_vars(Expire)
        /\ \A h \in Hosts : WF_vars(Take(h)) /\ WF_vars(Serve(h)) /\ WF_vars(Refresh(h)) /\ WF_vars(Restart(h))
        /\ \A r \in Reqs : WF_vars(Handle(r)) /\ WF_vars(Answer(r)) /\ WF_vars(Relay(r)) /\ WF_vars(Resend(r))

\* A request is forwarded once at most: never by the server that it was forwarded to.
SingleHop == \A r \in Reqs : hops[r] <= 1
\* Only the daemon that serves the live term applies a request.
HolderApplies == ~bad
\* No request is applied twice by forwarding.
AtMostOnce == \A r \in Reqs : applied[r] <= 1
\* A client gets the holder's answer, a 503 (Retry-After), or a reset; never a 409, which a worker takes for a refusal.
Answers == \A r \in Reqs : /\ ans[r] \in {"none", "ok", "503", "reset"}
                           /\ (ans[r] = "ok" => applied[r] = 1)
\* Every request that a client sent is answered.
Answered == \A r \in Reqs : st[r] # "new" ~> st[r] = "done"
====
