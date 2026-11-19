---- MODULE best_fold ----
\* The best of a track as a fold over the best rule's stored answers. P = piloop/plugins/campaign_type/kit/pool at this
\* commit: P/pool.py (Pool.ready, Pool.key, Pool.fold), P/judging.py (judge, waiting) and the per-campaign lock of
\* RunController.judge. The baseline is candidate 0. A candidate becomes ready once, at the end of the ready order; a
\* remeasure adds a sample, so its mean and every key that names it change; an exclusion takes a ready candidate out; a
\* change of the rule's table changes every key. A judge reads the step that the fold waits at, asks the rule outside
\* any session, then stores the answer under the key that it read. TLC checks it
\* (`tlc -config best_fold_fixed.cfg best_fold.tla`); tests/test_best_fold_model.py replays its graph against the code.
EXTENDS Naturals, Sequences, FiniteSets, TLC
CONSTANTS Js,        \* the judges that may run: the daemon's pass, and a scored candidate's thread
          LOCK,      \* one judge at a time for each campaign
          AT_ASKED   \* an answer is stored under the key that the judge asked, not the key that the fold waits at then
Cs == {1, 2}
Answers == {"keep", "refuse", "unanswered"}
G == 1  \* remeasures of each candidate
R == 1  \* changes of the rule's table
\* A candidate's mean after g remeasures: candidate 1 scores 2 then 0, candidate 2 scores 3 then 1, the baseline 1.
Mean(c, g) == CASE c = 1 -> (IF g = 0 THEN 2 ELSE 1) [] c = 2 -> (IF g = 0 THEN 3 ELSE 2)
MeanOf(c, g) == IF c = 0 THEN 1 ELSE Mean(c, g[c])
\* P:Pool.key: the rule's table and each side's samples. The baseline's samples never change here.
KeyOf(c, inc, g, r) == <<c, g[c], inc, IF inc = 0 THEN 0 ELSE g[inc], r>>
\* P:Pool.fold: from the baseline, each ready candidate that strictly beats the best so far needs an answer.
RECURSIVE Walk(_, _, _, _, _, _)
Walk(s, i, best, g, r, sto) ==
    IF i > Len(s) THEN [best |-> best, wait |-> <<>>, err |-> FALSE]
    ELSE LET c == s[i] IN
         IF Mean(c, g[c]) <= MeanOf(best, g) THEN Walk(s, i + 1, best, g, r, sto)
         ELSE LET k == KeyOf(c, best, g, r) IN
              IF k \notin DOMAIN sto THEN [best |-> best, wait |-> <<c, best, k>>, err |-> FALSE]
              ELSE IF sto[k] = "keep" THEN Walk(s, i + 1, c, g, r, sto)
              ELSE IF sto[k] = "refuse" THEN Walk(s, i + 1, best, g, r, sto)
              ELSE [best |-> best, wait |-> <<c, best, k>>, err |-> TRUE]
FoldOf(st, ord, g, r, sto) == Walk(SelectSeq(ord, LAMBDA c : st[c] = "ready"), 1, 0, g, r, sto)

VARIABLES status, order, gen, rv, store, judge, asked, seen, doubled, best, waitc, err, op, arg
vars == <<status, order, gen, rv, store, judge, asked, seen, doubled, best, waitc, err, op, arg>>
Fold == FoldOf(status, order, gen, rv, store)
\* P:judging.waiting: a step that the fold waits at and that the rule never answered.
Waits == Fold.wait # <<>> /\ ~Fold.err
\* What the replay reads after each step: the best, the candidate that the fold waits at (0: none), and whether the
\* rule did not answer there.
Show(st, ord, g, r, sto) == LET f == FoldOf(st, ord, g, r, sto) IN
    /\ best' = f.best /\ waitc' = (IF f.wait = <<>> THEN 0 ELSE f.wait[1]) /\ err' = f.err

Init == /\ status = [c \in Cs |-> "none"] /\ order = <<>> /\ gen = [c \in Cs |-> 0] /\ rv = 0
        /\ store = [k \in {} |-> "keep"] /\ judge = [j \in Js |-> "idle"] /\ asked = [j \in Js |-> <<>>]
        /\ seen = {} /\ doubled = FALSE /\ best = 0 /\ waitc = 0 /\ err = FALSE /\ op = "init" /\ arg = <<"-", 0, "-">>
\* Its evaluation confirms it: the measurement that made it ready has the newest id.
Ready(c) == /\ status[c] = "none" /\ status' = [status EXCEPT ![c] = "ready"] /\ order' = Append(order, c)
            /\ Show(status', order', gen, rv, store) /\ op' = "ready" /\ arg' = <<"-", c, "-">>
            /\ UNCHANGED <<gen, rv, store, judge, asked, seen, doubled>>
\* piloop candidate remeasure: one more sample.
Remeasure(c) == /\ status[c] = "ready" /\ gen[c] < G /\ gen' = [gen EXCEPT ![c] = @ + 1]
                /\ Show(status, order, gen', rv, store) /\ op' = "remeasure" /\ arg' = <<"-", c, "-">>
                /\ UNCHANGED <<status, order, rv, store, judge, asked, seen, doubled>>
\* piloop candidate exclude.
Exclude(c) == /\ status[c] = "ready" /\ status' = [status EXCEPT ![c] = "excluded"]
              /\ Show(status', order, gen, rv, store) /\ op' = "exclude" /\ arg' = <<"-", c, "-">>
              /\ UNCHANGED <<order, gen, rv, store, judge, asked, seen, doubled>>
\* An edit of [plugins.best_rule], such as its version, then piloop campaign sync.
Rule == /\ rv < R /\ rv' = rv + 1 /\ Show(status, order, gen, rv', store) /\ op' = "rule" /\ arg' = <<"-", 0, "-">>
        /\ UNCHANGED <<status, order, gen, store, judge, asked, seen, doubled>>
\* The daemon's pass, or a scored candidate, starts a judge when a step waits (RunController.judge). With LOCK, one
\* that starts while another runs waits for the lock: it starts after.
Start(j) == /\ judge[j] = "idle" /\ Waits /\ (LOCK => \A i \in Js : judge[i] = "idle")
            /\ judge' = [judge EXCEPT ![j] = "reading"] /\ op' = "start" /\ arg' = <<j, 0, "-">>
            /\ UNCHANGED <<status, order, gen, rv, store, asked, seen, doubled, best, waitc, err>>
\* P:judging.judge reads the step in a session of its own, then asks the rule; it returns when none waits.
Read(j) == /\ judge[j] = "reading"
           /\ IF Waits
              THEN /\ judge' = [judge EXCEPT ![j] = "asking"] /\ asked' = [asked EXCEPT ![j] = Fold.wait]
                   /\ seen' = seen \cup {Fold.wait[3]} /\ doubled' = (doubled \/ Fold.wait[3] \in seen)
                   /\ arg' = <<j, Fold.wait[1], "-">>
              ELSE /\ judge' = [judge EXCEPT ![j] = "idle"] /\ arg' = <<j, 0, "-">>
                   /\ UNCHANGED <<asked, seen, doubled>>
           /\ op' = "read" /\ UNCHANGED <<status, order, gen, rv, store, best, waitc, err>>
\* The rule answers, and the judge stores the answer, the first for its key. On unanswered it escalates and returns.
Answer(j, a) == /\ judge[j] = "asking"
                /\ LET k == IF AT_ASKED \/ ~Waits THEN asked[j][3] ELSE Fold.wait[3] IN
                     store' = IF k \in DOMAIN store THEN store ELSE store @@ (k :> a)
                /\ judge' = [judge EXCEPT ![j] = IF a = "unanswered" THEN "idle" ELSE "reading"]
                /\ asked' = [asked EXCEPT ![j] = <<>>]
                /\ Show(status, order, gen, rv, store') /\ op' = "answer" /\ arg' = <<j, asked[j][1], a>>
                /\ UNCHANGED <<status, order, gen, rv, seen, doubled>>
Done == /\ \A j \in Js : judge[j] = "idle" /\ UNCHANGED vars
Next == \/ \E c \in Cs : Ready(c) \/ Remeasure(c) \/ Exclude(c)
        \/ Rule
        \/ \E j \in Js : Start(j) \/ Read(j) \/ \E a \in Answers : Answer(j, a)
        \/ Done

\* Assumed: a started judge goes on, and the rule answers.
Live == \A j \in Js : WF_vars(Read(j)) /\ WF_vars(\E a \in Answers : Answer(j, a))
Spec == Init /\ [][Next]_vars /\ Live /\ \A j \in Js : WF_vars(Start(j))
\* Without the pass that starts a judge: Answered must fail, so it is not vacuous.
Unfair == Init /\ [][Next]_vars /\ Live

\* The best is the fold over the stored answers.
BestIsFold == best = Fold.best
\* No best but the baseline without a stored keep for it.
KeptBest == best = 0 \/ \E k \in DOMAIN store : k[1] = best /\ store[k] = "keep"
\* Every stored answer is the rule's to the case of its key, and no case is asked twice.
StoredWereAsked == DOMAIN store \subseteq seen
OneAskPerKey == ~doubled
\* One judge at a time for each campaign.
OneJudge == Cardinality({j \in Js : judge[j] # "idle"}) <= 1
\* Every candidate that the fold waits at is answered, or the rule's failure is stored and escalated.
Answered == \A c \in Cs : (waitc = c /\ ~err) ~> (waitc # c \/ err)
====
