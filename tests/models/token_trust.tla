---- MODULE token_trust ----
\* A server's trust in the tokens it verified (`Tokens`, piloop/net/tokens.py at 20946f603), and a revoke or a mint that
\* overlaps a call. One name; its token 1, and 2, which a mint puts in 1's place. Two calls, each once, the second
\* started after the first (they are alike). Time in ticks; a trust lasts TRUST of them. TLC:
\* `-config token_trust_current.cfg token_trust.tla`. tests/net/test_token_trust_model.py replays its graph.
EXTENDS Naturals
CONSTANTS COUNT, FORGET, TRUST   \* a read checks the forget count; when a revoke forgets: "last", "first" or "never"
VARIABLES db, trusted, pc, sec, stale, found, age, revoke, cut, mint, gone, op, arg
vars == <<db, trusted, pc, sec, stale, found, age, revoke, cut, mint, gone, op, arg>>
Secrets == {1, 2}
Calls == {1, 2}
Away == {"read", "store"}   \* a call between its first lock and its second

Init == /\ db = {1} /\ trusted = [x \in Secrets |-> 0] /\ pc = [c \in Calls |-> "idle"] /\ sec = [c \in Calls |-> 1]
        /\ stale = [c \in Calls |-> FALSE] /\ found = [c \in Calls |-> FALSE] /\ age = [c \in Calls |-> 0]
        /\ revoke = "idle" /\ cut = {} /\ mint = "idle" /\ gone = {} /\ op = "init" /\ arg = 0
\* tokens.py:175-180: count the forget, and drop each trust of the name. Only whether the count moved since a call's
\* start matters (tokens.py:167), so the model keeps that (`stale`), not the count.
Forgets == /\ trusted' = [x \in Secrets |-> 0] /\ stale' = [c \in Calls |-> stale[c] \/ pc[c] \in Away]
Calling == UNCHANGED <<pc, sec, found, age>>

Turn(c) == c = 1 \/ pc[1] # "idle"
\* tokens.py:159-163: under the lock, read the trust and the count; answer from a trust that has not ended.
Start(c, x) == LET hit == trusted[x] > 0 IN
               /\ pc[c] = "idle" /\ Turn(c) /\ sec' = [sec EXCEPT ![c] = x]
               /\ pc' = [pc EXCEPT ![c] = (IF hit THEN "done" ELSE "read")] /\ op' = (IF hit THEN "trusted" ELSE "start")
               /\ arg' = c /\ UNCHANGED <<db, trusted, stale, found, age, revoke, cut, mint, gone>>
\* tokens.py:164-165: the row, read in a session of its own, outside the lock.
Read(c) == /\ pc[c] = "read" /\ found' = [found EXCEPT ![c] = sec[c] \in db] /\ pc' = [pc EXCEPT ![c] = "store"]
           /\ op' = "read" /\ arg' = c /\ UNCHANGED <<db, trusted, stale, sec, age, revoke, cut, mint, gone>>
\* tokens.py:166-172: trust what it read unless it found nobody or a forget ran since; the trust ends `trust` after the
\* call's start. The TRUSTED cap (tokens.py:170) only clears trust, so it is left out.
Store(c) == LET keep == found[c] /\ TRUST > 0 /\ (~COUNT \/ ~stale[c]) IN
            /\ pc[c] = "store" /\ pc' = [pc EXCEPT ![c] = "done"]
            /\ trusted' = [trusted EXCEPT ![sec[c]] = IF keep THEN TRUST - age[c] ELSE 0]
            /\ stale' = [stale EXCEPT ![c] = FALSE] /\ found' = [found EXCEPT ![c] = FALSE] /\ age' = [age EXCEPT ![c] = 0]
            /\ op' = "store" /\ arg' = c /\ UNCHANGED <<db, sec, revoke, cut, mint, gone>>
\* tokens.py:191-196, and the API's route api.py:654-663: delete and commit, then forget. The revoke has returned once
\* its second step ran: `gone` holds what it deleted. FORGET = "first" forgets before it deletes; "never" not at all.
Deleted == db' = {} /\ cut' = db /\ UNCHANGED <<trusted, stale>>
RevokeFirst == /\ revoke = "idle" /\ revoke' = "deleted" /\ op' = "revoke_first" /\ arg' = 0
               /\ (IF FORGET = "first" THEN Forgets /\ UNCHANGED <<db, cut>> ELSE Deleted)
               /\ UNCHANGED <<mint, gone>> /\ Calling
RevokeSecond == /\ revoke = "deleted" /\ revoke' = "done" /\ op' = "revoke_second" /\ arg' = 0
                /\ CASE FORGET = "last" -> Forgets /\ gone' = gone \cup cut /\ UNCHANGED <<db, cut>>
                     [] FORGET = "first" -> Deleted /\ gone' = gone \cup db
                     [] OTHER -> gone' = gone \cup cut /\ UNCHANGED <<db, cut, trusted, stale>>
                /\ UNCHANGED <<mint>> /\ Calling
\* tokens.py:182-189: a mint deletes 1 and adds 2 in one commit, then forgets the name.
MintDelete == /\ mint = "idle" /\ db' = (db \ {1}) \cup {2} /\ mint' = "deleted" /\ op' = "mint_delete" /\ arg' = 0
              /\ UNCHANGED <<trusted, stale, gone, revoke, cut>> /\ Calling
MintForget == /\ mint = "deleted" /\ mint' = "done" /\ Forgets /\ gone' = gone \cup {1} /\ op' = "mint_forget"
              /\ arg' = 0 /\ UNCHANGED <<db, revoke, cut>> /\ Calling
\* Another process revokes 1 (tokens.py:96-99 on its own `Database`): nothing here forgets it, so 1 stops within the trust.
Elsewhere == /\ 1 \in db /\ db' = db \ {1} /\ op' = "elsewhere" /\ arg' = 0
             /\ UNCHANGED <<trusted, stale, revoke, cut, mint, gone>> /\ Calling
\* The clock (`Tokens.clock`), while a trust runs or a call is away.
Tick == /\ (\E x \in Secrets : trusted[x] > 0) \/ (\E c \in Calls : pc[c] \in Away /\ age[c] < TRUST)
        /\ trusted' = [x \in Secrets |-> IF trusted[x] > 0 THEN trusted[x] - 1 ELSE 0]
        /\ age' = [c \in Calls |-> IF pc[c] \in Away /\ age[c] < TRUST THEN age[c] + 1 ELSE age[c]]
        /\ op' = "tick" /\ arg' = 0 /\ UNCHANGED <<db, stale, pc, sec, found, revoke, cut, mint, gone>>
\* A graph for the tests leaves environment steps out: `MintDelete <- Off` in its cfg.
Off == FALSE
One == {1}                                     \* `Calls <- One`
Sequential(c) == c = 1 \/ pc[1] = "done"       \* `Turn <- Sequential`: the second call starts once the first ended
\* The end: both calls answered and nothing trusted. A revoke or a mint may still come; Done does not wait for one.
Done == /\ \A c \in Calls : pc[c] = "done" /\ \A x \in Secrets : trusted[x] = 0 /\ UNCHANGED vars

Next == \/ \E c \in Calls : (\E x \in Secrets : Start(c, x)) \/ Read(c) \/ Store(c)
        \/ RevokeFirst \/ RevokeSecond \/ MintDelete \/ MintForget \/ Elsewhere \/ Tick \/ Done
\* Assumed: a call under way finishes, and the clock moves. Revokes, mints and new calls may never come.
Finishing == \A c \in Calls : WF_vars(Read(c)) /\ WF_vars(Store(c))
Spec == Init /\ [][Next]_vars /\ WF_vars(Tick) /\ Finishing
Stopped == Init /\ [][Next]_vars /\ Finishing   \* a clock that may stop: EventuallyRefused must fail

\* A token revoked or replaced through this object is trusted no more once that returned, whatever call overlapped it.
NoTrustAfterRevoke == \A x \in gone : trusted[x] = 0
\* A token deleted anywhere, by another process too, is in the end trusted no more.
EventuallyRefused == \A x \in Secrets : <>[](x \notin db => trusted[x] = 0)
====
