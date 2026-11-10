---- MODULE forkserver ----
\* A relayed call forked from the warm template (`Forks`, piloop/plugins/executor/kit/forks.py at 20946f603; the template
\* is kit/template.py), with its timeouts, the template's death and the fallback to a process of its own. Two calls, two
\* templates at most. `runs` counts each start of the call's command. TLC: `-config forkserver_current.cfg forkserver.tla`.
EXTENDS Naturals
CONSTANT FIXED   \* a child runs its command only once the server, which read its pid, says go
VARIABLES tpl, cur, at, phase, child, runs, late, op, arg
vars == <<tpl, cur, at, phase, child, runs, late, op, arg>>
Calls == {1, 2}
Gens == {1, 2}
Ended == {"done", "fallback_done", "stopped"}
\* phase: the server's side of a call. child: "none", "queued" (in the template's socket), "forked" (its child runs,
\* and has not sent its pid), "running" (sent it; the command runs), "exited", "gone" (died before it sent its pid).
Init == /\ tpl = [g \in Gens |-> "none"] /\ cur = 0 /\ at = [c \in Calls |-> 0] /\ phase = [c \in Calls |-> "idle"]
        /\ child = [c \in Calls |-> "none"] /\ runs = [c \in Calls |-> 0] /\ late = FALSE /\ op = "init" /\ arg = 0
Fresh == CHOOSE g \in Gens : tpl[g] = "none"
Up(g) == g # 0 /\ tpl[g] = "up"

\* forks.py:160-178: poll the template, start one if there is none (:170, :189-204), send the call's request. No
\* template left to start is `_start`'s OSError (:201-203): Unforked, and the call runs as a process of its own.
Send(c) == /\ phase[c] = "idle" /\ arg' = c /\ UNCHANGED <<late, runs>>
           /\ IF Up(cur) THEN /\ at' = [at EXCEPT ![c] = cur] /\ child' = [child EXCEPT ![c] = "queued"]
                              /\ phase' = [phase EXCEPT ![c] = "sent"] /\ op' = "send" /\ UNCHANGED <<tpl, cur>>
              ELSE IF \E g \in Gens : tpl[g] = "none"
                   THEN /\ tpl' = [tpl EXCEPT ![Fresh] = "up"] /\ cur' = Fresh /\ at' = [at EXCEPT ![c] = Fresh]
                        /\ child' = [child EXCEPT ![c] = "queued"] /\ phase' = [phase EXCEPT ![c] = "sent"]
                        /\ op' = "start_send"
                   ELSE /\ phase' = [phase EXCEPT ![c] = "fallback"] /\ op' = "no_template"
                        /\ UNCHANGED <<tpl, cur, at, child>>
\* template.py:28-40: the template takes a request and forks its child. It dies at any time (a kill, an OOM; THREADED
\* at :32-34 is a death before the fork): a request still in its socket is lost with it.
Fork(c) == /\ child[c] = "queued" /\ Up(at[c]) /\ child' = [child EXCEPT ![c] = "forked"]
           /\ op' = "fork" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, phase, runs, late>>
Die(g) == /\ tpl[g] = "up" /\ tpl' = [tpl EXCEPT ![g] = "dead"] /\ op' = "template_dies" /\ arg' = g
          /\ UNCHANGED <<cur, at, phase, child, runs, late>>
\* template.py:63-75: the child sets up its session, sends its pid (:71), then runs the command (:75). A pid sent once
\* the server closed the call's socket fails (EPIPE, Python ignores SIGPIPE): the child dies. FIXED: it waits for go.
ChildStart(c) == LET closed == phase[c] = "stopped" \/ (FIXED /\ phase[c] = "stopping") IN
                 /\ child[c] = "forked" /\ child' = [child EXCEPT ![c] = IF closed THEN "gone" ELSE "running"]
                 /\ runs' = [runs EXCEPT ![c] = IF closed THEN @ ELSE @ + 1] /\ late' = (late \/ (~closed /\ phase[c] = "stopping"))
                 /\ op' = "child_start" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, phase>>
ChildDies(c) == /\ child[c] = "forked" /\ child' = [child EXCEPT ![c] = "gone"] /\ op' = "child_dies" /\ arg' = c
                /\ UNCHANGED <<tpl, cur, at, phase, runs, late>>
\* template.py:76-79: the command ends; the child sends its code, and its end of the socket closes as it exits.
ChildExit(c) == /\ child[c] = "running" /\ child' = [child EXCEPT ![c] = "exited"] /\ op' = "child_exit" /\ arg' = c
                /\ UNCHANGED <<tpl, cur, at, phase, runs, late>>
\* forks.py:142, :146: the pid came. forks.py:146-147 and :106-108: the reply's end closed with no pid, because no
\* process holds it: the call runs as a process of its own (`processes`, :64-68).
Started(c) == /\ phase[c] = "sent" /\ child[c] \in {"running", "exited"} /\ phase' = [phase EXCEPT ![c] = "started"]
              /\ op' = "started" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
Unforked(c) == /\ phase[c] = "sent" /\ (child[c] = "gone" \/ (child[c] = "queued" /\ tpl[at[c]] = "dead"))
               /\ phase' = [phase EXCEPT ![c] = "fallback"] /\ op' = "unforked" /\ arg' = c
               /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
Fallback(c) == /\ phase[c] = "fallback" /\ phase' = [phase EXCEPT ![c] = "fallback_done"]
               /\ runs' = [runs EXCEPT ![c] = @ + 1] /\ op' = "fallback" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, child, late>>
\* forks.py:148-158: the code, then the close: the call is answered.
Finish(c) == /\ phase[c] = "started" /\ child[c] = "exited" /\ phase' = [phase EXCEPT ![c] = "done"]
             /\ op' = "finish" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
\* forks.py:143-145, :180-187: no pid by the deadline. `_end` kills the template process, not a child that it forked,
\* then the call's socket closes as the exception leaves its block (:131), and the relay answers that the call was
\* stopped (relay.py:115-119).
TimeoutBefore(c) == LET g == at[c] IN
    /\ phase[c] = "sent" /\ child[c] \in {"queued", "forked", "gone"} /\ phase' = [phase EXCEPT ![c] = "stopping"]
    /\ tpl' = [tpl EXCEPT ![g] = IF @ = "up" THEN "dead" ELSE @] /\ cur' = (IF cur = g THEN 0 ELSE cur)
    /\ op' = "timeout_before" /\ arg' = c /\ UNCHANGED <<at, child, runs, late>>
Close(c) == /\ phase[c] = "stopping" /\ phase' = [phase EXCEPT ![c] = "stopped"] /\ op' = "close" /\ arg' = c
            /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
\* forks.py:154-157: no code by the deadline: kill the child's session (it has one of its own, template.py:63).
TimeoutAfter(c) == /\ phase[c] = "started" /\ child[c] = "running" /\ phase' = [phase EXCEPT ![c] = "stopped"]
                   /\ child' = [child EXCEPT ![c] = "exited"] /\ op' = "timeout_after" /\ arg' = c
                   /\ UNCHANGED <<tpl, cur, at, runs, late>>
\* Negative controls that a cfg swaps in: a call whose child died with no code runs again; a stop that kills nothing.
FinishOrFallback(c) == /\ phase[c] = "started" /\ child[c] = "exited" /\ arg' = c
                       /\ \/ phase' = [phase EXCEPT ![c] = "done"] /\ op' = "finish"
                          \/ phase' = [phase EXCEPT ![c] = "fallback"] /\ op' = "unforked"
                       /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
StopKillingNothing(c) == /\ phase[c] = "started" /\ child[c] = "running" /\ phase' = [phase EXCEPT ![c] = "stopped"]
                         /\ op' = "timeout_after" /\ arg' = c /\ UNCHANGED <<tpl, cur, at, child, runs, late>>
\* The end: both calls answered, and no child of theirs still runs its command.
Done == /\ \A c \in Calls : phase[c] \in Ended /\ child[c] # "running" /\ child[c] # "forked" /\ UNCHANGED vars

Server(c) == Send(c) \/ Started(c) \/ Unforked(c) \/ Fallback(c) \/ Finish(c) \/ Close(c)
Timeout(c) == TimeoutBefore(c) \/ TimeoutAfter(c)
Next == \/ \E c \in Calls : Server(c) \/ Fork(c) \/ ChildStart(c) \/ ChildDies(c) \/ ChildExit(c) \/ Timeout(c)
        \/ \E g \in Gens : Die(g) \/ Done
\* Assumed: the server's steps and the template's run, and each call's deadline comes. A child's start, its end and
\* any death are not fair: a command may hang until its deadline.
Spec == Init /\ [][Next]_vars /\ \A c \in Calls : WF_vars(Server(c)) /\ WF_vars(Fork(c)) /\ WF_vars(Timeout(c))
NoDeadline == Init /\ [][Next]_vars /\ \A c \in Calls : WF_vars(Server(c)) /\ WF_vars(Fork(c))  \* Answered must fail

\* A call's command runs at most once: forked, or as a process of its own.
NeverTwice == \A c \in Calls : runs[c] <= 1
\* A call whose child started is never run again as a fallback.
StartedNoFallback == \A c \in Calls : phase[c] \in {"fallback", "fallback_done"} => child[c] \notin {"running", "exited"}
\* A call that the server stops before it started never starts: the relay answers that it was stopped.
StoppedStaysStopped == ~late /\ \A c \in Calls : phase[c] = "stopped" => child[c] # "running"
\* Every call is answered.
Answered == \A c \in Calls : <>(phase[c] \in Ended)
====
