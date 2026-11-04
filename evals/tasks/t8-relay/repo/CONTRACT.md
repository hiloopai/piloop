# The relay's contract

The relay delivers the events that producers commit to the outbox to the sink. `sim.py` runs it on schedules that
a seed draws, `check.py` checks clauses 1 to 7 on what the store and the sink recorded, and `evaluate.py` measures
clause 8. A relay meets the contract when clauses 1 to 7 hold on every schedule that the world below allows, at any
rate up to the highest it states.

## Clauses

1. **No silent drop.** Every committed event has, by the end of the run, an effect at the sink or a dead letter.
2. **Publish once.** The sink applies no event's effect more than once.
3. **Per-key order.** The sink applies an event's effect only after every event of the same key with a lower id has
   its effect or its dead letter.
4. **Single writer.** Every save that the store applies, and every delivery that reaches the sink, comes from the
   process that holds the valid lease on that key at that tick.
5. **Compaction behind the cursors.** Every compaction leaves the floor at most one above the lowest cursor saved for
   any key, a key never saved counting as cursor 0.
6. **Restore equivalence.** For each event, the attempt numbers that reach the sink from its key's lease holder start
   at 1, and each is the one before it or one more, whatever crashed, paused or changed hands in between.
7. **Bounded retries.** No attempt is numbered above K = 4, and an event gets at most one dead letter, written only
   after its attempt K reached the sink and failed or timed out.
8. **Liveness** (measured, not pass/fail). The run drains: every transaction has ended and the outbox is empty. The
   drain tick is the measure; a run that has not drained at the cap counts the cap.

"Reaches the sink" means a delivery that the sink answered or let time out; a delivery refused with `Fenced` does not
reach it. A dead letter is written by `save(..., dead=[...])`. The cursor of a key is what its latest applied `save`
names.

## The world

Time is in ticks. The relay is `workers` worker processes and one compactor, each with its own `Client`
(`store.py`). A process has nothing but its calls: it shares no memory with another process, or with its own
incarnation before a crash.

- **Calls.** A store or clock call completes 1 to 2 ticks after it is issued, a delivery 2 to 6. Each is applied
  atomically when it completes. Calls that complete on the same tick are applied in an order the seed draws.
- **Producers.** A run has a fixed number of events, in transactions of 1 to 3, which start in 3 to 6 bursts. An id
  is allocated when its event is inserted; a transaction commits or aborts 1 to 30 ticks after its last insert, so
  events commit out of id order. Up to 20% of transactions abort.
- **Crashes.** Each process crashes up to 4 times per 1000 ticks, and every process at once (an outage) up to once
  per 1000 ticks. A call in flight at a crash is applied or not. The process restarts 5 to 30 ticks later, with fresh
  memory.
- **Pauses.** Each process pauses up to 4 times per 1000 ticks, for 20 to 100 ticks each. Its calls in flight complete
  when the pause ends, so they may be applied after its leases expired.
- **The sink.** An attempt fails, with no effect, with a probability up to 0.2; it times out with a probability up to
  0.1, and then half the time it has applied the effect, and it raises 12 ticks after it reached the sink. Up to 10% of
  the events are poison: every attempt at them fails.
- **Leases** last at most 40 ticks.
- **The cap.** A run stops at 3000 ticks with 2 workers, 3 keys and 24 events, and at 4000 with up to 4 workers,
  6 keys and 48 events.

These are the only faults. A schedule turns on some of them (`sim.SCENARIOS`) at its split's rates.
