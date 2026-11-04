"""The safety clauses of ``CONTRACT.md``, checked on what the store and the sink recorded in one run.

Nothing here reads what a relay reports about itself: only the commits, the attempts that reached the sink, the saves
and the compactions, with their global order (``seq``) and whether the writer held the key's lease at that moment.
"""

from sim import History

MAX_ATTEMPTS = 4
"""K: the attempts an event gets before its dead letter."""
CLAUSES = ('no_drop', 'once', 'order', 'fencing', 'compaction', 'restore', 'retries')
"""Clauses 1 to 7 of ``CONTRACT.md``, in order."""


def outcomes(history: History) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
    """For each event: the seq of its first effect, of its first dead letter, and of the first of the two."""
    effect: dict[int, int] = {}
    dead: dict[int, int] = {}
    for a in history.attempts:
        if a.effect:
            effect.setdefault(a.event, a.seq)
    for s in history.saves:
        for eid in s.dead:
            dead.setdefault(eid, s.seq)
    first = {**dead}
    for eid, seq in effect.items():
        first[eid] = min(seq, first.get(eid, seq))
    return effect, dead, first


def check(history: History) -> dict[str, str]:
    """The first violation of each clause, or ``''`` where it holds."""
    effect, _, first = outcomes(history)
    found = dict.fromkeys(CLAUSES, '')
    for c in history.commits:
        if c.id not in first:
            found['no_drop'] = found['no_drop'] or f'event {c.id} (key {c.key}) has no effect and no dead letter'
    found['once'] = once(history)
    found['order'] = order(history, effect, first)
    found['fencing'] = fencing(history)
    found['compaction'] = compaction(history)
    found['restore'] = restore(history)
    found['retries'] = retries(history)
    return found


def once(history: History) -> str:
    """Clause 2: no event's effect is applied twice."""
    seen: set[int] = set()
    for a in history.attempts:
        if a.effect:
            if a.event in seen:
                return f'event {a.event} took effect a second time, at tick {a.tick} (attempt {a.number})'
            seen.add(a.event)
    return ''


def order(history: History, effect: dict[int, int], first: dict[int, int]) -> str:
    """Clause 3: an event of a key takes effect only after every lower event of that key has an outcome."""
    by_key: dict[int, list[int]] = {}
    for c in history.commits:
        by_key.setdefault(c.key, []).append(c.id)
    for key, ids in by_key.items():
        latest, which = 0, 0
        for eid in sorted(ids):
            if eid in effect and effect[eid] < latest:
                return f'event {eid} of key {key} took effect before event {which} had an outcome'
            if eid in first and first[eid] > latest:
                latest, which = first[eid], eid
    return ''


def fencing(history: History) -> str:
    """Clause 4: every save and every delivery comes from the holder of the key's valid lease."""
    for s in history.saves:
        if not s.holder:
            return f'a save of key {s.key} at tick {s.tick} came from a process without its lease'
    for a in history.attempts:
        if not a.holder:
            return f'attempt {a.number} of event {a.event} at tick {a.tick} came from a process without its lease'
    return ''


def compaction(history: History) -> str:
    """Clause 5: the floor stays at most one above the lowest cursor saved for any key (0 for a key never saved)."""
    cursors = [0] * history.keys
    saves, i = history.saves, 0
    for c in history.compactions:
        while i < len(saves) and saves[i].seq < c.seq:
            cursors[saves[i].key] = saves[i].cursor
            i += 1
        lowest = min(cursors)
        if c.below > lowest + 1:
            key = cursors.index(lowest)
            return f'compacted below {c.below} at tick {c.tick}, past the saved cursor {lowest} of key {key}'
    return ''


def restore(history: History) -> str:
    """Clause 6: the attempt numbers of an event that reach the sink from its key's holder go 1, then up by 0 or 1."""
    last: dict[int, int] = {}
    for a in history.attempts:
        if not a.holder:
            continue
        before = last.get(a.event, 0)
        if a.number not in (before, before + 1) or a.number < 1:
            return f'event {a.event} got attempt {a.number} after attempt {before or "none"}, at tick {a.tick}'
        last[a.event] = a.number
    return ''


def retries(history: History) -> str:
    """Clause 7: at most K attempts, and one dead letter, written only after attempt K failed."""
    failed_k: dict[int, int] = {}
    for a in history.attempts:
        if a.number > MAX_ATTEMPTS:
            return f'event {a.event} got attempt {a.number}, above {MAX_ATTEMPTS}, at tick {a.tick}'
        if a.number == MAX_ATTEMPTS and a.outcome in ('failure', 'timeout'):
            failed_k.setdefault(a.event, a.seq)
    dead: set[int] = set()
    for s in history.saves:
        for eid in s.dead:
            if eid in dead:
                return f'event {eid} got a second dead letter, at tick {s.tick}'
            if failed_k.get(eid, 1 << 62) > s.seq:
                return f'event {eid} got a dead letter at tick {s.tick} before attempt {MAX_ATTEMPTS} failed'
            dead.add(eid)
    return ''
