---- MODULE replica ----
\* L5b: the bucket leader's terms, a Litestream replica per term (terms/<epoch>/), a restore at each take, the id lift.
\* Two hosts. A commit is <<epoch of its writer, id>>. The lease itself is checked in term.tla (L5a): here a take may
\* happen while the last holder still runs (a pause past its term, a fault), and that holder goes on as a stale writer.
\* A host's disk dies with it: the next writer is on another host. A prefix is a sequence of segments: its first
\* snapshot, then the WAL frames that Litestream ships in order. TLC: `-deadlock -config replica_<cfg>.cfg replica.tla`.
EXTENDS Naturals, FiniteSets, Sequences
CONSTANTS MAXE, MAXC, FAULTS,
          SRC,         \* where a new term restores from: the newest earlier prefix with a snapshot ("newest"),
                       \* epoch - 1 ("previous"), or the source that the term record pins ("pinned")
          LIFT,        \* the term's first transaction lifts the id sequences to epoch * B (decision 17)
          SNAP_FIRST,  \* a term serves only once its first snapshot is in its prefix
          PIN_FIRST,   \* "pinned": a term serves only once the record names what it restored
          SHARED,      \* every term replicates to one prefix, not terms/<epoch>/ (decision 20)
          OLDER        \* a restore re-reads the record and goes on only while it is still its term's: else the term exits
VARIABLES phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale,
          dup, faults, op, arg
vars == <<phase, ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale,
          dup, faults, op, arg>>
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

\* bucket.py `attempt` (L5a). With the last holder still live, the take is a fault (its pause outlasted the term).
Take(h) == LET holding == {g \in Hosts : ep[g] = top /\ phase[g] \in Live} IN
           /\ phase[h] = "standby" /\ top < MAXE
           /\ \/ holding = {} /\ Keep(faults)
              \/ holding # {} /\ faults < FAULTS /\ faults' = faults + 1
           /\ top' = top + 1 /\ ep' = [ep EXCEPT ![h] = top + 1] /\ phase' = [phase EXCEPT ![h] = "taken"]
           /\ mine' = [mine EXCEPT ![h] = {}] /\ shipped' = [shipped EXCEPT ![h] = {}]
           /\ snapped' = [snapped EXCEPT ![h] = FALSE] /\ pinned' = [pinned EXCEPT ![h] = FALSE] /\ op' = "take"
           /\ arg' = h /\ Keep(<<db, seq, from, rep, src, started, issued, durable, stale, dup>>)
\* `litestream restore` from the source, then the first transaction: the lift. "pinned" reads the record's source: a
\* whole prefix (ALL), or a prefix up to the position at which the term that pinned it restored.
Snapshotted(e) == Len(rep[P(e)]) > 0
Source(e) == CASE SRC = "newest" -> <<MaxOf({f \in 1..(e - 1) : Snapshotted(f)}), ALL>>
               [] SRC = "previous" -> <<e - 1, ALL>>
               [] OTHER -> src
Restore(h) == LET s == Source(ep[h]) IN LET got == IF s[1] = 0 THEN {} ELSE Upto(rep[P(s[1])], s[2]) IN
              /\ phase[h] = "taken" /\ (ep[h] = top \/ ~OLDER) /\ db' = [db EXCEPT ![h] = got]
              /\ from' = [from EXCEPT ![h] = <<s[1], IF s[1] = 0 THEN 0 ELSE IF s[2] = ALL THEN Len(rep[P(s[1])]) ELSE s[2]>>]
              /\ seq' = [seq EXCEPT ![h] = IF LIFT THEN MaxOf(Ids(got) \cup {ep[h] * B}) ELSE MaxOf(Ids(got))]
              /\ phase' = [phase EXCEPT ![h] = "restored"] /\ op' = "restore" /\ arg' = h
              /\ Keep(<<ep, mine, shipped, snapped, pinned, rep, src, top, started, issued, durable, stale, dup, faults>>)
\* A renewal of the term record that names what the term restored: refused (If-Match) once another term took.
Pin(h) == /\ SRC = "pinned" /\ phase[h] = "restored" /\ ~pinned[h] /\ ep[h] = top
          /\ src' = (IF snapped[h] THEN <<ep[h], ALL>> ELSE from[h]) /\ pinned' = [pinned EXCEPT ![h] = TRUE]
          /\ op' = "pin" /\ arg' = h
          /\ Keep(<<phase, ep, db, seq, mine, shipped, snapped, from, rep, top, started, issued, durable, stale, dup, faults>>)
\* `litestream replicate` starts with a snapshot of the whole database into the term's prefix. Under "pinned", the
\* holder's next renewal names its own prefix, whole, as the source: refused once another term took.
Snapshot(h) == /\ phase[h] \in Live /\ ~snapped[h] /\ (pinned[h] \/ SRC # "pinned") /\ snapped' = [snapped EXCEPT ![h] = TRUE]
               /\ rep' = [rep EXCEPT ![P(ep[h])] = <<db[h]>>] /\ shipped' = [shipped EXCEPT ![h] = mine[h]]
               /\ src' = (IF SRC = "pinned" /\ ep[h] = top /\ pinned[h] THEN <<ep[h], ALL>> ELSE src)
               /\ op' = "snapshot" /\ arg' = h
               /\ Keep(<<phase, ep, db, seq, mine, pinned, from, top, started, issued, durable, stale, dup, faults>>)
Serve(h) == /\ phase[h] = "restored" /\ (snapped[h] \/ ~SNAP_FIRST) /\ (pinned[h] \/ ~PIN_FIRST \/ SRC # "pinned")
            /\ phase' = [phase EXCEPT ![h] = "holding"] /\ started' = [started EXCEPT ![ep[h]] = TRUE]
            /\ op' = "serve" /\ arg' = h
            /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, issued, durable, stale, dup, faults>>)
\* A write that hands an id to a worker (a lease, a cursor). A stale writer still writes until it exits.
Commit(h) == LET c == <<ep[h], seq[h] + 1>> IN
             /\ phase[h] \in {"holding", "stale"} /\ Cardinality(mine[h]) < MAXC
             /\ db' = [db EXCEPT ![h] = @ \cup {c}] /\ seq' = [seq EXCEPT ![h] = @ + 1]
             /\ mine' = [mine EXCEPT ![h] = @ \cup {c}] /\ issued' = issued \cup {c[2]}
             /\ dup' = (dup \/ c[2] \in issued) /\ stale' = (IF Newer(ep[h]) THEN stale \cup {c} ELSE stale)
             /\ op' = "commit" /\ arg' = h
             /\ Keep(<<phase, ep, shipped, snapped, pinned, from, rep, src, top, started, durable, faults>>)
\* Litestream ships the WAL in order, after its snapshot, asynchronously: an unshipped tail is lost with the host.
Ship(h) == LET left == mine[h] \ shipped[h] IN LET c == CHOOSE x \in left : \A y \in left : x[2] <= y[2] IN
           /\ phase[h] \in {"holding", "stale"} /\ snapped[h] /\ left # {}
           /\ rep' = [rep EXCEPT ![P(ep[h])] = Append(@, {c})] /\ shipped' = [shipped EXCEPT ![h] = @ \cup {c}]
           /\ durable' = (IF ep[h] = top THEN durable \cup {c} ELSE durable) /\ op' = "ship" /\ arg' = h
           /\ Keep(<<phase, ep, db, seq, mine, snapped, pinned, from, src, top, started, issued, stale, dup, faults>>)
\* The holder of an older epoch learns that it lost its term: it goes on (stale) until it exits.
Lose(h) == /\ phase[h] \in Live /\ (pinned[h] \/ phase[h] = "holding" \/ SRC # "pinned") /\ ep[h] < top /\ phase' = [phase EXCEPT ![h] = "stale"] /\ op' = "lose" /\ arg' = h
           /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
Gone(h) == /\ phase' = [phase EXCEPT ![h] = "down"] /\ db' = [db EXCEPT ![h] = {}]
Exit(h) == /\ phase[h] = "stale" /\ Gone(h) /\ op' = "exit" /\ arg' = h
           /\ Keep(<<ep, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
\* bucket.py `restore`: another term took over before this one restored, or before its pin landed (If-Match): the term
\* exits, and never serves.
Abandon(h) == /\ OLDER /\ ep[h] < top
              /\ (phase[h] = "taken" \/ (phase[h] = "restored" /\ SRC = "pinned" /\ ~pinned[h]))
              /\ Gone(h) /\ op' = "abandon" /\ arg' = h
              /\ Keep(<<ep, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)
Crash(h) == /\ phase[h] \notin {"down", "standby"} /\ faults < FAULTS /\ faults' = faults + 1 /\ Gone(h)
            /\ op' = "crash" /\ arg' = h
            /\ Keep(<<ep, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup>>)
Restart(h) == /\ phase[h] = "down" /\ phase' = [phase EXCEPT ![h] = "standby"] /\ op' = "restart" /\ arg' = h
              /\ Keep(<<ep, db, seq, mine, shipped, snapped, pinned, from, rep, src, top, started, issued, durable, stale, dup, faults>>)

Next == \E h \in Hosts : Take(h) \/ Restore(h) \/ Abandon(h) \/ Pin(h) \/ Snapshot(h) \/ Serve(h) \/ Commit(h) \/ Ship(h)
                         \/ Lose(h) \/ Exit(h) \/ Crash(h) \/ Restart(h)
Spec == Init /\ [][Next]_vars

Current(h) == phase[h] \in Live /\ ~Newer(ep[h])   \* a holder that no later serving term made stale
\* No id that a worker holds is handed out again: lease fences and event cursors stay unique.
NoIdReuse == ~dup
\* A commit shipped while its writer was the newest term survives every later restore.
DurableKept == \A h \in Hosts : Current(h) => {c \in durable : c[1] < ep[h]} \subseteq db[h]
\* No commit that a stale writer made after a newer term began to serve comes back in a later term's database.
NoStaleCommit == \A h \in Hosts : Current(h) => db[h] \cap stale = {}
====
