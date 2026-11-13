---- MODULE replica ----
\* L5b: the bucket leader's terms, a Litestream replica per term (terms/<epoch>/), a restore at each take, the id lift.
\* Two hosts. A commit is <<epoch of its writer, id>>. The lease itself is checked in term.tla (L5a): here a take may
\* happen while the last holder still runs (a pause past its term, a fault), and that holder goes on as a stale writer.
\* A host's disk dies with it: the next writer is on another host. A prefix is a sequence of segments: its first
\* snapshot, then the WAL frames that Litestream ships in order. TLC: `-deadlock -config replica_<cfg>.cfg replica.tla`.
\* With SWITCH, a disk also outlives a daemon that stops cleanly, and the home goes once from `bucket` to `local` and
\* back: the `local` leader serves one host's database, counts its epoch from that database's terms, and never reads or
\* writes the record. The operator's move back runs `bucket` on that host first (one replica, then two).
EXTENDS Naturals, FiniteSets, Sequences
CONSTANTS MAXE, MAXC, FAULTS,
          SRC,         \* where a new term restores from: the newest earlier prefix with a snapshot ("newest"),
                       \* epoch - 1 ("previous"), or the source that the term record pins ("pinned")
          LIFT,        \* the term's first transaction lifts the id sequences to epoch * B (decision 17)
          SNAP_FIRST,  \* a term serves only once its first snapshot is in its prefix
          PIN_FIRST,   \* "pinned": a term serves only once the record names what it restored
          SHARED,      \* every term replicates to one prefix, not terms/<epoch>/ (decision 20)
          OLDER,       \* a restore re-reads the record and goes on only while it is still its term's: else the term exits
          SWITCH,      \* disks that outlive a clean stop, and one move of the home to `local` and back
          KEEP         \* when a restore keeps this host's database: "never" (it restores the source), "source" (the
                       \* database holds a term newer than the source's), "record" (newer than the record's epoch)
VARIABLES phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale,
          dup, faults, op, arg,
          dterm,       \* the newest term in each host's database (`terms.newest_in`): 0 for none
          prev,        \* the record's epoch that each host's last take read
          mode,        \* the home's leader provider: "bucket" or "local"
          lw,          \* the commits of the `local` term
          lterm,       \* the `local` term's epoch, 0 before it
          lhost,       \* the host that served it
          kterm        \* the first `bucket` term after it, 0 before it
vars == <<phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale,
          dup, faults, op, arg, dterm, prev, mode, lw, lterm, lhost, kterm>>
Switch == <<dterm, prev, mode, lw, lterm, lhost, kterm>>
Hosts == {1, 2}
Epochs == 1..MAXE
B == MAXC + 1                                   \* `epoch << 32`, scaled down: a term's ids are epoch*B+1 .. epoch*B+MAXC
ALL == 99                                       \* a pinned position past every segment: the whole prefix
P(e) == IF SHARED THEN 1 ELSE e                 \* the prefix that term e replicates to
Live == {"restored", "holding"}
MaxOf(S) == IF S = {} THEN 0 ELSE CHOOSE x \in S : \A y \in S : y <= x
Ids(S) == {c[2] : c \in S}
Upto(s, n) == UNION {s[i] : i \in 1..(IF n < Len(s) THEN n ELSE Len(s))}
Newer(e) == \E f \in Epochs : f > e /\ started[f]   \* a later term serves: e's writer is stale from now
Keep(v) == UNCHANGED v

Init == /\ phase = [h \in Hosts |-> "standby"] /\ ep = [h \in Hosts |-> 0] /\ db = [h \in Hosts |-> {}]
        /\ seq = [h \in Hosts |-> 0] /\ mine = [h \in Hosts |-> {}] /\ shipped = [h \in Hosts |-> {}]
        /\ snapped = [h \in Hosts |-> FALSE] /\ pinned = [h \in Hosts |-> FALSE] /\ from = [h \in Hosts |-> <<0, 0>>]
        /\ rep = [e \in Epochs |-> <<>>] /\ src = <<0, 0>> /\ top = 0 /\ started = [e \in Epochs |-> FALSE]
        /\ issued = {} /\ durable = {} /\ stale = {} /\ dup = FALSE /\ faults = 0 /\ op = "init" /\ arg = 0
        /\ dterm = [h \in Hosts |-> 0] /\ prev = [h \in Hosts |-> 0] /\ mode = "bucket" /\ lw = {} /\ lterm = 0
        /\ lhost = 0 /\ kterm = 0

\* bucket.py `attempt` (L5a): one above the record's epoch and the newest term of the host's database. With the last
\* holder still live, the take is a fault (its pause outlasted the term). The first take after the move back to
\* `bucket` is on the host that served `local`.
Take(h) == LET holding == {g \in Hosts : ep[g] = top /\ phase[g] \in Live} IN
           LET e == (IF dterm[h] > top THEN dterm[h] ELSE top) + 1 IN
           /\ mode = "bucket" /\ phase[h] = "standby" /\ e <= MAXE
           /\ (lterm = 0 \/ kterm # 0 \/ h = lhost)
           /\ \/ holding = {} /\ Keep(faults)
              \/ holding # {} /\ faults < FAULTS /\ faults' = faults + 1
           /\ top' = e /\ ep' = [ep EXCEPT ![h] = e] /\ phase' = [phase EXCEPT ![h] = "taken"]
           /\ prev' = [prev EXCEPT ![h] = top] /\ kterm' = (IF lterm # 0 /\ kterm = 0 THEN e ELSE kterm)
           /\ mine' = [mine EXCEPT ![h] = {}] /\ shipped' = [shipped EXCEPT ![h] = {}]
           /\ snapped' = [snapped EXCEPT ![h] = FALSE] /\ pinned' = [pinned EXCEPT ![h] = FALSE] /\ op' = "take"
           /\ arg' = h /\ Keep(<<db, seq, from, rep, src, started, issued, durable, stale, dup, dterm, mode, lw, lterm, lhost>>)
\* `litestream restore` from the source, then the first transaction: the lift. "pinned" reads the record's source: a
\* whole prefix (ALL), or a prefix up to the position at which the term that pinned it restored. With no source, or
\* when KEEP says the host's database is the newer one, the term keeps that database and pins the source as it was.
Snapshotted(e) == Len(rep[P(e)]) > 0
Source(e) == CASE SRC = "newest" -> <<MaxOf({f \in 1..(e - 1) : Snapshotted(f)}), ALL>>
               [] SRC = "previous" -> <<e - 1, ALL>>
               [] OTHER -> src
Fresher(h) == CASE KEEP = "record" -> dterm[h] > prev[h]
               [] KEEP = "source" -> dterm[h] > src[1]
               [] OTHER -> FALSE
Restore(h) == LET s == Source(ep[h]) IN
              LET kept == s[1] = 0 \/ (SRC = "pinned" /\ Fresher(h)) IN
              LET got == IF kept THEN db[h] ELSE Upto(rep[P(s[1])], s[2]) IN
              /\ phase[h] = "taken" /\ (ep[h] = top \/ ~OLDER) /\ db' = [db EXCEPT ![h] = got]
              /\ from' = [from EXCEPT ![h] = IF s[1] = 0 THEN <<0, 0>> ELSE IF kept THEN s
                                             ELSE <<s[1], IF s[2] = ALL THEN Len(rep[P(s[1])]) ELSE s[2]>>]
              \* a kept database keeps its own sequences (`sqlite_sequence`); a restored one has its source's
              /\ seq' = [seq EXCEPT ![h] = MaxOf(Ids(got) \cup (IF kept THEN {seq[h]} ELSE {}) \cup (IF LIFT THEN {ep[h] * B} ELSE {}))]
              /\ dterm' = [dterm EXCEPT ![h] = ep[h]]
              /\ phase' = [phase EXCEPT ![h] = "restored"] /\ op' = "restore" /\ arg' = h
              /\ Keep(<<ep, mine, shipped, snapped, pinned, rep, src, top, started, issued, durable, stale, dup, faults>>)
              /\ Keep(<<prev, mode, lw, lterm, lhost, kterm>>)
\* A renewal of the term record that names what the term restored: refused (If-Match) once another term took.
Pin(h) == /\ SRC = "pinned" /\ phase[h] = "restored" /\ ~pinned[h] /\ ep[h] = top
          /\ src' = (IF snapped[h] THEN <<ep[h], ALL>> ELSE from[h]) /\ pinned' = [pinned EXCEPT ![h] = TRUE]
          /\ op' = "pin" /\ arg' = h
          /\ Keep(<<phase, ep, db, seq, mine, shipped, snapped, from, rep, top, started, issued, durable, stale, dup, faults>>)
          /\ Keep(Switch)
\* `litestream replicate` starts with a snapshot of the whole database into the term's prefix. Under "pinned", the
\* holder's next renewal names its own prefix, whole, as the source: refused once another term took.
Snapshot(h) == /\ phase[h] \in Live /\ ~snapped[h] /\ (pinned[h] \/ SRC # "pinned") /\ snapped' = [snapped EXCEPT ![h] = TRUE]
               /\ rep' = [rep EXCEPT ![P(ep[h])] = <<db[h]>>] /\ shipped' = [shipped EXCEPT ![h] = mine[h]]
               /\ src' = (IF SRC = "pinned" /\ ep[h] = top /\ pinned[h] THEN <<ep[h], ALL>> ELSE src)
               /\ op' = "snapshot" /\ arg' = h
               /\ Keep(<<phase, ep, db, seq, mine, pinned, from, top, started, issued, durable, stale, dup, faults>>)
               /\ Keep(Switch)
Serve(h) == /\ phase[h] = "restored" /\ (snapped[h] \/ ~SNAP_FIRST) /\ (pinned[h] \/ ~PIN_FIRST \/ SRC # "pinned")
            /\ phase' = [phase EXCEPT ![h] = "holding"] /\ started' = [started EXCEPT ![ep[h]] = TRUE]
            /\ op' = "serve" /\ arg' = h
            /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, issued, durable, stale, dup, faults>>)
            /\ Keep(Switch)
\* A write that hands an id to a worker (a lease, a cursor). A stale writer still writes until it exits.
Commit(h) == LET c == <<ep[h], seq[h] + 1>> IN
             /\ phase[h] \in {"holding", "stale", "local"} /\ Cardinality(mine[h]) < MAXC
             /\ db' = [db EXCEPT ![h] = @ \cup {c}] /\ seq' = [seq EXCEPT ![h] = @ + 1]
             /\ mine' = [mine EXCEPT ![h] = @ \cup {c}] /\ issued' = issued \cup {c[2]}
             /\ dup' = (dup \/ c[2] \in issued) /\ stale' = (IF Newer(ep[h]) THEN stale \cup {c} ELSE stale)
             /\ lw' = (IF phase[h] = "local" THEN lw \cup {c} ELSE lw) /\ op' = "commit" /\ arg' = h
             /\ Keep(<<phase, ep, shipped, snapped, pinned, from, rep, src, top, started, durable, faults>>)
             /\ Keep(<<dterm, prev, mode, lterm, lhost, kterm>>)
\* Litestream ships the WAL in order, after its snapshot, asynchronously: an unshipped tail is lost with the host.
Ship(h) == LET left == mine[h] \ shipped[h] IN LET c == CHOOSE x \in left : \A y \in left : x[2] <= y[2] IN
           /\ phase[h] \in {"holding", "stale"} /\ snapped[h] /\ left # {}
           /\ rep' = [rep EXCEPT ![P(ep[h])] = Append(@, {c})] /\ shipped' = [shipped EXCEPT ![h] = @ \cup {c}]
           /\ durable' = (IF ep[h] = top THEN durable \cup {c} ELSE durable) /\ op' = "ship" /\ arg' = h
           /\ Keep(<<phase, ep, db, seq, mine, snapped, pinned, from, src, top, started, issued, stale, dup, faults>>)
           /\ Keep(Switch)
\* The holder of an older epoch learns that it lost its term: it goes on (stale) until it exits.
Lose(h) == /\ phase[h] \in Live /\ (pinned[h] \/ phase[h] = "holding" \/ SRC # "pinned") /\ ep[h] < top /\ phase' = [phase EXCEPT ![h] = "stale"] /\ op' = "lose" /\ arg' = h
           /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
           /\ Keep(Switch)
Gone(h) == /\ phase' = [phase EXCEPT ![h] = "down"] /\ db' = [db EXCEPT ![h] = {}] /\ seq' = [seq EXCEPT ![h] = 0]
           /\ dterm' = [dterm EXCEPT ![h] = 0] /\ Keep(<<prev, mode, lw, lterm, lhost, kterm>>)
Exit(h) == /\ phase[h] = "stale" /\ Gone(h) /\ op' = "exit" /\ arg' = h
           /\ Keep(<<ep, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
\* bucket.py `restore`: another term took over before this one restored, or before its pin landed (If-Match): the term
\* exits, and never serves.
Abandon(h) == /\ OLDER /\ ep[h] < top
              /\ (phase[h] = "taken" \/ (phase[h] = "restored" /\ SRC = "pinned" /\ ~pinned[h]))
              /\ Gone(h) /\ op' = "abandon" /\ arg' = h
              /\ Keep(<<ep, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
\* Not the `local` term's host: its disk is the home's only copy, and with it the home is gone, not moved.
Crash(h) == /\ phase[h] \notin {"down", "standby", "local"} /\ faults < FAULTS /\ faults' = faults + 1 /\ Gone(h)
            /\ op' = "crash" /\ arg' = h
            /\ Keep(<<ep, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup>>)
Restart(h) == /\ phase[h] = "down" /\ phase' = [phase EXCEPT ![h] = "standby"] /\ op' = "restart" /\ arg' = h
              /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
              /\ Keep(Switch)

\* SWITCH: a daemon that stops cleanly keeps its disk, and starts again as a standby. A holder's stop ships the rest
\* first (Litestream's last frames): its replica is whole and named.
Stop(h) == /\ SWITCH
           /\ \/ phase[h] = "local"
              \/ phase[h] = "holding" /\ snapped[h] /\ mine[h] \subseteq shipped[h] /\ src = <<ep[h], ALL>>
           /\ phase' = [phase EXCEPT ![h] = "standby"] /\ op' = "stop" /\ arg' = h
           /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
           /\ Keep(Switch)
Running == {h \in Hosts : phase[h] \notin {"standby", "down"}}
\* The operator moves the home to `local` once no daemon runs, then back once its daemon stopped.
ToLocal == /\ SWITCH /\ mode = "bucket" /\ lterm = 0 /\ Running = {} /\ mode' = "local" /\ op' = "to_local" /\ arg' = 0
           /\ Keep(<<phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
           /\ Keep(<<dterm, prev, lw, lterm, lhost, kterm>>)
ToBucket == /\ mode = "local" /\ lterm # 0 /\ Running = {} /\ mode' = "bucket" /\ op' = "to_bucket" /\ arg' = 0
            /\ Keep(<<phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
            /\ Keep(<<dterm, prev, lw, lterm, lhost, kterm>>)
\* local.py `take`: one above the newest term of the host's database, which holds the last `bucket` term's.
Local(h) == LET e == dterm[h] + 1 IN
            /\ mode = "local" /\ lterm = 0 /\ phase[h] = "standby" /\ dterm[h] = top /\ top > 0 /\ e <= MAXE
            /\ phase' = [phase EXCEPT ![h] = "local"] /\ ep' = [ep EXCEPT ![h] = e] /\ started' = [started EXCEPT ![e] = TRUE]
            /\ dterm' = [dterm EXCEPT ![h] = e] /\ mine' = [mine EXCEPT ![h] = {}] /\ lterm' = e /\ lhost' = h
            /\ op' = "local" /\ arg' = h
            /\ Keep(<<db, seq, shipped, snapped, pinned, from, rep, src, top, issued, durable, stale, dup, faults>>)
            /\ Keep(<<prev, mode, lw, kterm>>)

Next == \/ \E h \in Hosts : Take(h) \/ Restore(h) \/ Abandon(h) \/ Pin(h) \/ Snapshot(h) \/ Serve(h) \/ Commit(h) \/ Ship(h)
                            \/ Lose(h) \/ Exit(h) \/ Crash(h) \/ Restart(h) \/ Stop(h) \/ Local(h)
        \/ ToLocal \/ ToBucket
Spec == Init /\ [][Next]_vars

Current(h) == phase[h] \in Live /\ ~Newer(ep[h])   \* a holder that no later serving term made stale
\* No id that a worker holds is handed out again: lease fences and event cursors stay unique.
NoIdReuse == ~dup
\* A commit shipped while its writer was the newest term survives every later restore.
DurableKept == \A h \in Hosts : Current(h) => {c \in durable : c[1] < ep[h]} \subseteq db[h]
\* No commit that a stale writer made after a newer term began to serve comes back in a later term's database.
NoStaleCommit == \A h \in Hosts : Current(h) => db[h] \cap stale = {}
\* The first `bucket` term after the home ran under `local` holds every commit of the `local` term.
SwitchKept == \A h \in Hosts : phase[h] \in Live /\ ep[h] = kterm /\ kterm # 0 => lw \subseteq db[h]
====
