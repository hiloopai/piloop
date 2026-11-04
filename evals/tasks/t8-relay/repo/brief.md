# Make an outbox relay correct, then fast

`relay.py` is an outbox relay. Producers commit events to an outbox in a durable store; the relay's workers lease
the keys, deliver each key's events to a sink, retry failures, dead-letter an event that keeps failing, and save
their cursors; a compactor deletes what has been delivered. `sim.py` runs it in a simulated world of seeded
schedules: crashes, outages, pauses, sink failures and timeouts, poison events, and transactions that commit out of
order.

Make the relay meet `CONTRACT.md` on every schedule that its world allows, then make it drain faster.

## Rules

- Change `relay.py`, and add modules beside it if you like. Do not edit `sim.py`, `store.py`, `check.py`,
  `evaluate.py`, `dev.json` or `CONTRACT.md`: the final version is scored with fresh copies of them.
- The relay talks to the world only through its `Client` (`store.py`). It uses only the standard library modules
  that `evaluate.py` allows (`ALLOWED`), and does not reach into the interpreter, the simulator or an imported module:
  the audit in `evaluate.py` refuses such a relay, and a refused relay scores 0.
- The final version is run on more seeds, with 4 workers and 6 keys, at the highest fault rates that `CONTRACT.md`
  states. A relay that holds on the dev schedules only by luck loses there.

## Measure

`python3 evaluate.py` runs the relay on the 1200 dev schedules (300 seeds, 4 scenarios; 2 workers, 3 keys) in a few
seconds, and prints `score` (higher is better) with `safety`, `speed` and each clause's share. `safety` is the mean
over clauses 1 to 7 of the share of schedules where the clause held. `speed` is the geometric mean, over the schedules
where all seven held, of a reference relay's drain tick over yours. `score = safety * (0.8 + 0.2 * speed)`.
`report.txt` names the first schedule that breaks each clause; `python3 evaluate.py --trace <seed> <scenario>` prints
what the store and the sink recorded in it.

## Where it stands

The starting relay scores about 0.57 on dev. A relay that holds every clause and drains as fast as the reference
scores 1; a faster one scores more.
