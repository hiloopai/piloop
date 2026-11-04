"""The relay's world: the durable store, the sink, and the calls a relay process makes on them.

A relay never holds the store or the sink. Each of its processes gets a ``Client``, ``ctx``, whose methods return a
``Call`` that the process awaits: ``events = await ctx.read(key, after, 10)``. Awaiting a call hands control to the
simulator (``sim.py``), which applies it atomically at a later tick and resumes the process with its result or its
exception. Every call is a scheduling point: between two calls of one process, the others run.

``Store`` and ``Sink`` hold the state and apply the calls. They record what they apply, with the tick, the global
order (``seq``) and whether the caller held the lease of the key it wrote for; ``check.py`` reads only these records.
"""

from __future__ import annotations  # Python 3.13 evaluates annotations eagerly: Call refers to itself

import json
from collections.abc import Generator, Sequence
from typing import NamedTuple

MAX_TTL = 40
"""The longest lease, in ticks."""
MAX_LIMIT = 100
"""The most events one read returns."""
MAX_STATE = 65536
"""The largest saved state of one key, in bytes of JSON."""

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


class Event(NamedTuple):
    """A committed event of the outbox: its id, allocated when its transaction inserted it, its key, and its commit tick."""

    id: int
    key: int
    at: int


class Saved(NamedTuple):
    """What the store keeps for one key: its committed cursor and the rest of the relay's state."""

    cursor: int
    state: dict[str, Json]


class Lease(NamedTuple):
    """A lease on a name, held by the process that acquired it until tick ``expires`` (excluded).

    ``token`` grows with every grant of the name: a lease is valid while its token is the name's latest and the tick is
    before ``expires``.
    """

    name: int | str
    token: int
    expires: int


class Fenced(Exception):
    """A write was refused: the lease passed as ``fence`` is not the caller's valid lease on the key."""


class SinkError(Exception):
    """A delivery did not report success."""


class SinkFailure(SinkError):
    """The sink refused the delivery: it had no effect."""


class SinkTimeout(SinkError):
    """The delivery timed out: the sink may or may not have applied it."""


class Call[T]:
    """One call to the store, the sink or the clock, applied by the simulator when a process awaits it."""

    __slots__ = ('args', 'kind')

    def __init__(self, kind: str, *args: object) -> None:
        self.kind = kind
        self.args = args

    def __await__(self) -> Generator[Call[T], object, T]:
        result = yield self
        return result  # ty: ignore[invalid-return-type]


class Client:
    """What one relay process can do. ``slot`` is the process's number, the same after a restart."""

    def __init__(self, slot: int, workers: int, keys: Sequence[int]) -> None:
        self.slot = slot
        """``0`` to ``workers - 1`` for a worker; ``workers`` for the compactor."""
        self.workers = workers
        """How many worker processes run."""
        self.keys = tuple(keys)
        """Every key an event can have."""

    def now(self) -> Call[int]:
        """The current tick."""
        return Call('now')

    def sleep(self, ticks: int) -> Call[None]:
        """Do nothing for ``ticks`` ticks (at least 1)."""
        return Call('sleep', ticks)

    def changes(self) -> Call[int]:
        """The outbox's version: it grows with every commit and every abort."""
        return Call('changes')

    def wait(self, since: int | None = None, timeout: int = 50) -> Call[int]:
        """Sleep until the outbox's version passes ``since``, or for ``timeout`` ticks; return the version.

        With ``since`` None, it waits for the next commit or abort after the call is applied.
        """
        return Call('wait', since, timeout)

    def read(self, key: int, after: int, limit: int) -> Call[list[Event]]:
        """The committed events of ``key`` with an id above ``after``, by id, at most ``limit`` (up to ``MAX_LIMIT``).

        Ids are allocated when a transaction inserts an event, and it becomes visible when its transaction commits, so
        a read can see an event before one with a lower id that commits later. Compacted events are gone.
        """
        return Call('read', key, after, limit)

    def horizon(self) -> Call[int]:
        """The lowest id of an event in a transaction that is still open, or the next id to allocate if none is.

        Every id below it belongs to an event that is committed or aborted, for good.
        """
        return Call('horizon')

    def compact(self, below: int) -> Call[None]:
        """Delete every event with an id below ``below`` from the outbox, and every one that commits there later."""
        return Call('compact', below)

    def acquire(self, name: int | str, ttl: int) -> Call[Lease | None]:
        """A lease on ``name`` for ``ttl`` ticks (up to ``MAX_TTL``), or None while another valid lease holds it."""
        return Call('acquire', name, ttl)

    def renew(self, lease: Lease, ttl: int) -> Call[Lease | None]:
        """``lease`` extended to ``ttl`` ticks from now, with the same token; None if it is no longer valid."""
        return Call('renew', lease, ttl)

    def release(self, lease: Lease) -> Call[None]:
        """End ``lease`` now, if it is still valid."""
        return Call('release', lease)

    def load(self, key: int) -> Call[Saved]:
        """The cursor and state last saved for ``key``, a copy; ``Saved(0, {})`` if none was."""
        return Call('load', key)

    def save(
        self,
        key: int,
        cursor: int,
        state: dict[str, Json] | None = None,
        dead: Sequence[int] = (),
        fence: Lease | None = None,
    ) -> Call[None]:
        """Replace the saved cursor and state of ``key``, and write a dead letter for each event id in ``dead``.

        All of it is applied atomically, or none. ``cursor`` is the key's committed cursor, which the compaction clause
        of ``CONTRACT.md`` reads; ``state`` (default ``{}``) is anything else the relay keeps, as JSON. With ``fence``,
        the write is applied only if ``fence`` is the caller's valid lease on ``key``; it raises ``Fenced`` otherwise.
        """
        return Call('save', key, cursor, state, tuple(dead), fence)

    def deliver(self, event: int, attempt: int, idem: str | None = None, fence: Lease | None = None) -> Call[str]:
        """Deliver event ``event`` to the sink, as attempt number ``attempt``; return ``'ok'`` or ``'duplicate'``.

        With ``idem``, the sink applies the effect only the first time it sees that key, and answers ``'duplicate'``
        after. With ``fence``, it delivers only if ``fence`` is the caller's valid lease on the event's key, and raises
        ``Fenced`` otherwise. It raises ``SinkFailure`` (no effect) or ``SinkTimeout`` (perhaps an effect).
        """
        return Call('deliver', event, attempt, idem, fence)


class Commit(NamedTuple):
    """An event committed; ``lost`` when it committed below the compaction floor and was deleted at once."""

    seq: int
    tick: int
    id: int
    key: int
    lost: bool


class Attempt(NamedTuple):
    """A delivery that reached the sink (a fenced one does not)."""

    seq: int
    tick: int
    event: int
    number: int
    holder: bool
    """The caller held the valid lease on the event's key."""
    outcome: str
    """``ok``, ``duplicate``, ``failure`` or ``timeout``."""
    effect: bool
    """The sink applied the event's effect."""


class Save(NamedTuple):
    """A save the store applied."""

    seq: int
    tick: int
    key: int
    holder: bool
    cursor: int
    dead: tuple[int, ...]


class Compaction(NamedTuple):
    """A compaction the store applied: the floor rose to ``below``."""

    seq: int
    tick: int
    below: int


class Grant(NamedTuple):
    """The store's lease on one name: who holds it, until when."""

    holder: int
    token: int
    expires: int


class Store:
    """The durable store: the outbox, the leases, and each key's saved state and dead letters."""

    def __init__(self) -> None:
        self.next_id = 1
        self.open: dict[int, list[int]] = {}
        """Open transactions: their ids, by transaction."""
        self.key_of: dict[int, int] = {}
        self.outbox: dict[int, dict[int, int]] = {}
        """The visible events of each key, in commit order, with their commit ticks."""
        self.floor = 0
        self.leases: dict[int | str, Grant] = {}
        self.tokens = 0
        self.states: dict[int, tuple[int, str]] = {}
        self.version = 0
        self.commits: list[Commit] = []
        self.saves: list[Save] = []
        self.compactions: list[Compaction] = []

    def holds(self, caller: int, key: int | str, tick: int, fence: Lease | None = None) -> bool:
        """Whether ``caller`` holds a valid lease on ``key`` at ``tick`` (and, with ``fence``, that it is this one)."""
        grant = self.leases.get(key)
        if grant is None or grant.holder != caller or tick >= grant.expires:
            return False
        return fence is None or (fence.name == key and fence.token == grant.token)

    def insert(self, txn: int, key: int) -> int:
        """Allocate the next id to a new event of ``key`` in open transaction ``txn``."""
        eid = self.next_id
        self.next_id += 1
        self.open.setdefault(txn, []).append(eid)
        self.key_of[eid] = key
        return eid

    def commit(self, txn: int, seq: int, tick: int) -> list[Commit]:
        """Make the events of ``txn`` visible."""
        out = []
        for eid in self.open.pop(txn, []):
            lost = eid < self.floor
            if not lost:
                self.outbox.setdefault(self.key_of[eid], {})[eid] = tick
            out.append(Commit(seq, tick, eid, self.key_of[eid], lost))
        self.commits += out
        self.version += 1
        return out

    def abort(self, txn: int) -> None:
        """Drop the events of ``txn``: their ids stay unused."""
        self.open.pop(txn, None)
        self.version += 1

    def horizon(self) -> int:
        return min((ids[0] for ids in self.open.values()), default=self.next_id)

    def read(self, key: int, after: int, limit: int) -> list[Event]:
        events = self.outbox.get(key, {})
        found = sorted(eid for eid in events if eid > after)
        return [Event(eid, key, events[eid]) for eid in found[: max(0, min(limit, MAX_LIMIT))]]

    def compact(self, below: int, seq: int, tick: int) -> None:
        """Raise the floor to ``below``."""
        if below > self.floor:
            self.floor = below
            for events in self.outbox.values():
                for eid in [eid for eid in events if eid < below]:
                    del events[eid]
            self.compactions.append(Compaction(seq, tick, below))

    def acquire(self, caller: int, name: int | str, ttl: int, tick: int) -> Lease | None:
        grant = self.leases.get(name)
        if grant is not None and tick < grant.expires:
            return None
        self.tokens += 1
        expires = tick + max(1, min(ttl, MAX_TTL))
        self.leases[name] = Grant(caller, self.tokens, expires)
        return Lease(name, self.tokens, expires)

    def renew(self, caller: int, lease: Lease, ttl: int, tick: int) -> Lease | None:
        if not self.holds(caller, lease.name, tick, lease):
            return None
        expires = tick + max(1, min(ttl, MAX_TTL))
        self.leases[lease.name] = Grant(caller, lease.token, expires)
        return Lease(lease.name, lease.token, expires)

    def release(self, caller: int, lease: Lease, tick: int) -> None:
        if self.holds(caller, lease.name, tick, lease):
            self.leases[lease.name] = Grant(caller, lease.token, tick)

    def load(self, key: int) -> Saved:
        cursor, state = self.states.get(key, (0, '{}'))
        return Saved(cursor, json.loads(state))

    def save(self, caller: int, key: int, cursor: int, state: str, dead: tuple[int, ...], seq: int, tick: int) -> None:
        """Apply a save whose fence, if any, the caller has already passed."""
        self.states[key] = (cursor, state)
        self.saves.append(Save(seq, tick, key, self.holds(caller, key, tick), cursor, dead))


class Sink:
    """The downstream system: it applies each delivered event's effect, at most once per idempotency key."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.attempts: list[Attempt] = []

    def deliver(
        self, event: int, number: int, idem: str | None, holder: bool, outcome: str, applied: bool, seq: int, tick: int
    ) -> str:
        """Record an attempt whose fate the simulator drew: ``outcome`` before dedup, ``applied`` for a timeout."""
        effect = False
        if outcome in ('ok', 'timeout') and (outcome == 'ok' or applied):
            if idem is not None and idem in self.seen:
                outcome = 'duplicate' if outcome == 'ok' else outcome
            else:
                effect = True
                if idem is not None:
                    self.seen.add(idem)
        self.attempts.append(Attempt(seq, tick, event, number, holder, outcome, effect))
        return outcome


def encode(state: object) -> str:
    """``state`` as the JSON the store keeps; raises ``ValueError`` when it is not a JSON object or is too large."""
    if state is None:
        return '{}'
    if not isinstance(state, dict):
        raise ValueError('a saved state is a dict')
    try:
        text = json.dumps(state, sort_keys=True, allow_nan=False)
    except TypeError as e:
        raise ValueError(f'a saved state must be JSON: {e}') from e
    if len(text) > MAX_STATE:
        raise ValueError(f'a saved state is at most {MAX_STATE} bytes of JSON, not {len(text)}')
    return text
