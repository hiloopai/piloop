"""An outbox relay: it delivers every event that producers commit to the outbox to the sink, in order per key.

``sim.py`` runs ``workers`` copies of ``worker`` and one ``compactor``, each as its own process with a ``Client``
(``ctx``, see ``store.py``). A process keeps nothing across a crash: it restarts from what it saved in the store.

Each key is served by one worker at a time, the holder of the key's lease. Worker ``s`` prefers the keys ``k`` with
``k % workers == s``, and helps with other free keys while it has nothing of its own to do. For each key it holds, a
worker reads the events after the key's cursor, delivers them in id order, retries a failed delivery a few ticks
later, dead-letters an event after ``MAX_ATTEMPTS`` failed attempts, and saves the cursor. The compactor deletes
events once they are old enough to have been delivered.
"""

from store import MAX_LIMIT, Client, Lease, SinkError

MAX_ATTEMPTS = 4
"""Attempts before an event is dead-lettered: ``CONTRACT.md``'s K."""
TTL = 40
"""Lease length, in ticks."""
RENEW_BELOW = 20
"""A lease is renewed when fewer ticks than this remain."""
BATCH = 20
"""Events per read."""
RETRY_TICKS = 4
"""Ticks before a failed delivery is tried again."""
IDLE_TICKS = 1000
"""How long a worker with nothing to do sleeps, unless a commit wakes it. Its leases may lapse meanwhile."""
RETENTION = 2000
"""Ticks after which an event has been delivered, so the compactor may delete it."""
COMPACT_EVERY = 10
"""Ticks between two passes of the compactor."""


class Held:
    """A key that this worker holds: its lease, its cursor (the last event handled), and its retries.

    ``failures`` counts the failed attempts of the events being retried, by event id.
    """

    __slots__ = ('cursor', 'failures', 'lease', 'retry_at')

    def __init__(self, lease: Lease, cursor: int, failures: dict[str, int]) -> None:
        self.lease = lease
        self.cursor = cursor
        self.failures = failures
        self.retry_at = 0


class Worker:
    """One worker process."""

    def __init__(self, ctx: Client) -> None:
        self.ctx = ctx
        self.held: dict[int, Held] = {}

    def mine(self, key: int) -> bool:
        return key % self.ctx.workers == self.ctx.slot

    async def run(self) -> None:
        while True:
            await self.claim()
            busy = False
            for key in list(self.held):
                busy = await self.drain(key) or busy
            if not busy:
                await self.ctx.wait(timeout=await self.idle())

    async def idle(self) -> int:
        """Ticks to sleep: up to the first retry due, and at most ``IDLE_TICKS``."""
        now = await self.ctx.now()
        return max(1, min([IDLE_TICKS, *(h.retry_at - now for h in self.held.values() if h.retry_at > now)]))

    async def claim(self) -> None:
        """Renew the leases that run low, and take the free keys: this worker's own first."""
        for key, held in list(self.held.items()):
            if not await self.owned(held):
                del self.held[key]
        for key in sorted(self.ctx.keys, key=lambda k: not self.mine(k)):
            if key not in self.held:
                lease = await self.ctx.acquire(key, TTL)
                if lease is not None:
                    saved = await self.ctx.load(key)
                    self.held[key] = Held(lease, saved.cursor, {})

    async def owned(self, held: Held) -> bool:
        """Whether the lease is still ours, renewed when it runs low."""
        now = await self.ctx.now()
        if held.lease.expires - now >= RENEW_BELOW:
            return True
        lease = await self.ctx.renew(held.lease, TTL)
        if lease is None:
            return False
        held.lease = lease
        return True

    async def commit(self, key: int, held: Held, dead: tuple[int, ...] = ()) -> None:
        """Save the key's cursor, with a dead letter for each event of ``dead``."""
        await self.ctx.save(key, held.cursor, dead=dead)

    async def drain(self, key: int) -> bool:
        """One pass over ``key``: deliver the new events in id order, then save the cursor. True if there were any."""
        held = self.held[key]
        if held.retry_at > await self.ctx.now():
            return False
        events = await self.ctx.read(key, held.cursor, BATCH)
        if not events:
            if not self.mine(key):  # hand a helped key back to its own worker
                await self.ctx.release(held.lease)
                del self.held[key]
            return False
        for event in events:
            if not await self.owned(held):
                del self.held[key]
                return True
            name = str(event.id)
            attempt = held.failures.get(name, 0) + 1
            try:
                await self.ctx.deliver(event.id, attempt=attempt)
            except SinkError:
                held.failures[name] = attempt
                if attempt < MAX_ATTEMPTS:
                    held.retry_at = await self.ctx.now() + RETRY_TICKS
                    break
                await self.commit(key, held, dead=(event.id,))  # now, before the key's next event goes out
                continue
            held.failures.pop(name, None)
            held.cursor = event.id
        if await self.owned(held):
            await self.commit(key, held)
        else:
            del self.held[key]
        return True


async def worker(ctx: Client) -> None:
    """A worker process: from its start, or its restart after a crash."""
    await Worker(ctx).run()


async def compactor(ctx: Client) -> None:
    """The compactor process: it deletes the events committed more than ``RETENTION`` ticks ago."""
    while True:
        now = await ctx.now()
        below = await ctx.horizon()  # nothing at or above it may go: it may not have committed yet
        for key in ctx.keys:
            young = [e.id for e in await ctx.read(key, 0, MAX_LIMIT) if e.at > now - RETENTION]
            below = min([below, *young])
        await ctx.compact(below)
        await ctx.sleep(COMPACT_EVERY)
