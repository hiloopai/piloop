---- MODULE lease_replay ----
\* The parts of lease.tla's graph that tests/test_lease_model.py replays: the whole graph has 10^5 states. No answer
\* of a heartbeat is lost (a silence is the heartbeats a worker does not send), and no write is forced.
\* Alone: w1 alone, with time and the requeue, but not lease 2. Crashes: the same, with w1's crash and no lost answer.
\* Race: both workers, without time, so with neither an expiry nor a crash.
EXTENDS lease
Due(w) == \E l \in Held(w) : Valid(l, w) /\ left[l] < TOL
Common == /\ op' # "heartbeat_lost"
          /\ op' = "heartbeat" /\ arg'[2] = 1 => Due(arg'[1])
Alone == Common /\ arg'[1] # "w2" /\ op' # "crash" /\ snap'["w1"] # 2
Crashes == Common /\ arg'[1] # "w2" /\ op' \notin {"acq_read_lost", "acq_write_lost"} /\ snap'["w1"] # 2
Race == Common /\ op' \notin {"tick", "crash"}
====
