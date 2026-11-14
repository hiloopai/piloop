---- MODULE liveness ----
\* A stuck agent ends by a liveness policy (`[plugins.liveness]`). One idea, SLOTS run slots, two agents, each the
\* implementer of a run of its own, and a quiet clock 0..Q per agent: ticks since its lease was acquired or its last
\* output, capped at Q (`quiet_for`). Paths under piloop/: A = agents/results.py, K = plugins/role/consumers.py,
\* L = plugins/liveness/probes.py, C = plugins/campaign_type/kit/runs/controller.py, S = .../kit/runs/stop.py. The role
\* timeout (an hour) is past the clock: an agent that writes forever is bounded by WRITES instead. TLC checks it
\* (`tlc -config liveness_fixed.cfg liveness.tla`); tests/test_liveness_model.py replays its graph against the code.
EXTENDS Naturals, FiniteSets
CONSTANTS POLICY, COMMIT_FIRST, SCORE_FIRST, FROM_ACQUIRE, WAKE_ON_START, START_RECHECK, SLOTS, Q, WRITES
\* POLICY: the pass asks the policy (current: no policy). Fixes: the `ending` commit before the kill (K:_end); the idea
\* opens once its run is scored (C:_settle); the clock starts at the acquire (L); a launch wakes the pass (K:WINDING);
\* the policy rechecks an agent that waits for its acquire (L).
Agents == 1..2
VARIABLES idea, run, ag, acq, quiet, outs, first, ends, claims, scored, killed, due, woken, early, crashes, op, arg
vars == <<idea, run, ag, acq, quiet, outs, first, ends, claims, scored, killed, due, woken, early, crashes, op, arg>>
Open == {"queued", "running"}  Asked == {"starting", "running"}  Holds == {"starting", "running", "ending"}
Counting(a) == IF FROM_ACQUIRE THEN (ag[a] = "starting" /\ acq[a]) \/ ag[a] = "running" ELSE ag[a] \in Asked
Stalled(a) == ((ag[a] = "starting" /\ acq[a]) \/ ag[a] = "running") /\ quiet[a] = Q
Claiming(a) == claims[a] /\ run[a] \in Open \cup {"scoring"}
Fired(a) == woken[a] \/ (due[a] /\ (quiet[a] = Q \/ ~Counting(a)))
Init == /\ idea = "open" /\ run = [a \in Agents |-> "none"] /\ ag = [a \in Agents |-> "none"]
        /\ acq = [a \in Agents |-> FALSE] /\ quiet = [a \in Agents |-> 0] /\ outs = [a \in Agents |-> 0]
        /\ first = [a \in Agents |-> "-"] /\ ends = [a \in Agents |-> "-"] /\ claims = [a \in Agents |-> FALSE]
        /\ scored = [a \in Agents |-> FALSE] /\ killed = [a \in Agents |-> FALSE] /\ due = [a \in Agents |-> FALSE]
        /\ woken = [a \in Agents |-> FALSE] /\ early = FALSE /\ crashes = 0 /\ op = "init" /\ arg = 0
Step(name, a) == op' = name /\ arg' = a
\* optimization/planner.py:221-241, campaign_type/consumers.py:587: a free slot queues a run, and the pass launches its
\* implementer (`agent_session.started`, which wakes the pass with WAKE_ON_START).
Launch(a) == /\ run[a] = "none" /\ \A b \in Agents : b < a => run[b] # "none"
             /\ Cardinality({b \in Agents : run[b] \in Open}) < SLOTS
             /\ run' = [run EXCEPT ![a] = "running"] /\ ag' = [ag EXCEPT ![a] = "starting"]
             /\ quiet' = [quiet EXCEPT ![a] = 0] /\ woken' = [woken EXCEPT ![a] = WAKE_ON_START] /\ Step("launch", a)
             /\ UNCHANGED <<idea, acq, outs, first, ends, claims, scored, killed, due, early, crashes>>
\* executor/kit/leases.py:819-831: a worker takes the agent's lease (`acquired_at`). No event wakes the pass.
Acquire(a) == /\ ag[a] = "starting" /\ ~acq[a] /\ acq' = [acq EXCEPT ![a] = TRUE] /\ quiet' = [quiet EXCEPT ![a] = 0]
              /\ Step("acquire", a) /\ UNCHANGED <<idea, run, ag, outs, first, ends, claims, scored, killed, due, woken, early, crashes>>
\* A:560-561: the agent runs (`agent_session.running`, which wakes the pass). Not fair: a clone or a first transfer hangs.
Running(a) == /\ ag[a] = "starting" /\ acq[a] /\ ~killed[a] /\ ag' = [ag EXCEPT ![a] = "running"]
              /\ quiet' = [quiet EXCEPT ![a] = 0] /\ woken' = [woken EXCEPT ![a] = TRUE] /\ Step("running", a)
              /\ UNCHANGED <<idea, run, acq, outs, first, ends, claims, scored, killed, due, early, crashes>>
\* A:840-851: a snapshot of its session file (`SessionSaved`). It wakes nothing. At most WRITES of them.
Output(a) == /\ ag[a] = "running" /\ ~killed[a] /\ outs[a] < WRITES /\ outs' = [outs EXCEPT ![a] = @ + 1]
             /\ quiet' = [quiet EXCEPT ![a] = 0] /\ Step("output", a)
             /\ UNCHANGED <<idea, run, ag, acq, first, ends, claims, scored, killed, due, woken, early, crashes>>
\* Time passes on the clock of each agent that it counts for.
Tick == /\ \E a \in Agents : Counting(a) /\ quiet[a] < Q
        /\ quiet' = [a \in Agents |-> IF Counting(a) /\ quiet[a] < Q THEN quiet[a] + 1 ELSE quiet[a]] /\ Step("tick", 0)
        /\ UNCHANGED <<idea, run, ag, acq, outs, first, ends, claims, scored, killed, due, woken, early, crashes>>
\* campaign_type/kit/backlog/ops.py:179-202: its agent claims the open idea for its run (`piloop idea claim`).
Claim(a) == /\ ag[a] = "running" /\ run[a] = "running" /\ ~killed[a] /\ ~claims[a] /\ idea = "open"
            /\ idea' = "claimed" /\ claims' = [claims EXCEPT ![a] = TRUE] /\ Step("claim", a)
            /\ UNCHANGED <<run, ag, acq, quiet, outs, first, ends, scored, killed, due, woken, early, crashes>>
\* K:wind_down, L:check: a pass that an event (`woken`) or its recheck (`due`) woke asks the policy about the agent. At
\* `quiet_for` it answers Stalled: `end_agent(..., RunEnd.STALLED)` sets the row `ending` with `AgentEnded`, and the kill
\* follows the commit. Without COMMIT_FIRST the kill comes first and the commit in a later step, if the row is open.
PolicyEnd(a) ==
  \/ /\ POLICY /\ ag[a] \in Asked /\ Fired(a) /\ Counting(a) /\ quiet[a] = Q /\ ~killed[a]
     /\ first' = [first EXCEPT ![a] = IF @ = "-" THEN "cull" ELSE @] /\ early' = (early \/ ~acq[a])
     /\ due' = [due EXCEPT ![a] = FALSE] /\ woken' = [woken EXCEPT ![a] = FALSE]
     /\ IF COMMIT_FIRST THEN ag' = [ag EXCEPT ![a] = "ending"] /\ UNCHANGED killed
                        ELSE killed' = [killed EXCEPT ![a] = TRUE] /\ UNCHANGED ag
  \/ /\ killed[a] /\ ag[a] \in Asked /\ ag' = [ag EXCEPT ![a] = "ending"] /\ UNCHANGED <<first, early, due, woken, killed>>
\* K:wind_down, L:check: else it answers Alive, with its recheck: when the agent's clock would reach `quiet_for`, or
\* START_RECHECK, `start_within` ahead for one that waits for its acquire.
Recheck(a) == /\ POLICY /\ ag[a] \in Asked /\ Fired(a) /\ ~(Counting(a) /\ quiet[a] = Q) /\ ~killed[a]
              /\ woken' = [woken EXCEPT ![a] = FALSE] /\ due' = [due EXCEPT ![a] = Counting(a) \/ START_RECHECK]
              /\ Step("recheck", a) /\ UNCHANGED <<idea, run, ag, acq, quiet, outs, first, ends, claims, scored, killed, early, crashes>>
PolicyStep(a) == PolicyEnd(a) /\ Step("cull", a) /\ UNCHANGED <<idea, run, acq, quiet, outs, ends, claims, scored, crashes>>
\* campaign_type/kit/runs/ops.py:55-73: the agent ends its run (`piloop run end gave_up`), refused once it is `ending`.
AgentRunEnd(a) == /\ ag[a] = "running" /\ run[a] = "running" /\ first[a] = "-" /\ ~killed[a]
                  /\ first' = [first EXCEPT ![a] = "agent"] /\ Step("run_end", a)
                  /\ UNCHANGED <<idea, run, ag, acq, quiet, outs, ends, claims, scored, killed, due, woken, early, crashes>>
\* A:_agent, C:_finish/_ran/run_outcome/_close: its process ended (a kill, or an exit after its run end), its report
\* closes its row and then its run, which is scored now: the worker's run end, else `AgentEnded.end`, else the
\* process's failure. Without SCORE_FIRST the idea opens at the close (S:release).
Report(a) == LET e == IF first[a] = "agent" THEN "gave_up" ELSE IF ag[a] = "ending" THEN "stalled" ELSE "failed" IN
  /\ \/ ag[a] = "ending"
     \/ ag[a] \in Asked /\ killed[a]
     \/ ag[a] = "running" /\ first[a] = "agent"
  /\ ag' = [ag EXCEPT ![a] = "closed"] /\ run' = [run EXCEPT ![a] = "scoring"] /\ ends' = [ends EXCEPT ![a] = e]
  /\ due' = [due EXCEPT ![a] = FALSE] /\ woken' = [woken EXCEPT ![a] = FALSE]
  /\ idea' = (IF ~SCORE_FIRST /\ claims[a] /\ idea = "claimed" THEN "open" ELSE idea) /\ Step("report", a)
  /\ UNCHANGED <<acq, quiet, outs, first, claims, scored, killed, early, crashes>>
\* C:1221-1226: its submissions are scored.
Score(a) == /\ run[a] = "scoring" /\ ~scored[a] /\ scored' = [scored EXCEPT ![a] = TRUE] /\ Step("score", a)
            /\ UNCHANGED <<idea, run, ag, acq, quiet, outs, first, ends, claims, killed, due, woken, early, crashes>>
\* C:_settle, S:release: the claim ends, and a claimed idea opens again, with a note that says how the run ended.
Settle(a) == /\ run[a] = "scoring" /\ scored[a] /\ run' = [run EXCEPT ![a] = "closed"]
             /\ idea' = (IF claims[a] /\ idea = "claimed" THEN "open" ELSE idea) /\ Step("settle", a)
             /\ UNCHANGED <<ag, acq, quiet, outs, first, ends, claims, scored, killed, due, woken, early, crashes>>
\* daemon/main.py, daemon/loop.py: the daemon restarts, at most once and not fair. Its rechecks go, and the new
\* process runs every consumer once.
Restart == /\ crashes < 1 /\ crashes' = 1 /\ due' = [a \in Agents |-> FALSE] /\ woken' = [a \in Agents |-> ag[a] \in Asked]
           /\ Step("restart", 0) /\ UNCHANGED <<idea, run, ag, acq, quiet, outs, first, ends, claims, scored, killed, early>>
\* The end: every run is closed.
Done == \A a \in Agents : run[a] = "closed" /\ UNCHANGED vars
Next == \/ \E a \in Agents : Launch(a) \/ Acquire(a) \/ Running(a) \/ Output(a) \/ Claim(a) \/ PolicyStep(a)
                             \/ Recheck(a) \/ AgentRunEnd(a) \/ Report(a) \/ Score(a) \/ Settle(a)
        \/ Tick \/ Restart \/ Done
\* Assumed: the daemon's steps happen, a worker takes each lease, time passes, and a harness that can write, writes.
\* An agent's start, its claim and its run end are its own and not fair: an agent hangs. Restarts are not fair.
Daemon == /\ WF_vars(Tick)
          /\ \A a \in Agents : WF_vars(Launch(a)) /\ WF_vars(Acquire(a)) /\ WF_vars(Report(a)) /\ WF_vars(Score(a))
                               /\ WF_vars(Settle(a)) /\ WF_vars(Recheck(a))
FairPolicy == \A a \in Agents : WF_vars(PolicyStep(a))  FairOutput == \A a \in Agents : WF_vars(Output(a))
Spec == Init /\ [][Next]_vars /\ Daemon /\ FairPolicy /\ FairOutput
UnfairOutputSpec == Init /\ [][Next]_vars /\ Daemon /\ FairPolicy  UnfairPolicySpec == Init /\ [][Next]_vars /\ Daemon /\ FairOutput

NoIdeaLost == idea = "claimed" => \E a \in Agents : Claiming(a)
NoDoubleRun == Cardinality({a \in Agents : Claiming(a)}) <= 1
FirstEndStands == \A a \in Agents : ends[a] # "-" => /\ (first[a] = "agent" => ends[a] = "gave_up")
                                                      /\ (first[a] = "cull" => ends[a] = "stalled")
CullOnlyQuiet == ~early
SlotBound == Cardinality({a \in Agents : ag[a] \in Holds}) <= SLOTS
StalledSlotFreed == \A a \in Agents : Stalled(a) ~> ag[a] = "closed"
ClaimEnds == idea = "claimed" ~> idea # "claimed"
\* The graph of the tests: no restart, which changes no row.
NoRestart == crashes = 0
====
