"""Episode 4's five ways to hold a critical section, and what each one is worth.

Read this file as an argument, because that is what the episode is.

Episodes 1 to 3 all ended the same way: something waited, or something died.
Every one of those answers worked because ONE DATABASE COULD SEE EVERY LOCK.
The moment the critical section spans a call to somebody else's system, that
assumption is gone, and the lock has to leave the database.

A lock outside the database needs a timeout, because the holder can die and
nobody would ever release it. **A lock with a timeout is a lease**, and the
difference is the whole episode: nothing tells the holder when the lease ran
out. It carries on, perfectly confident, doing work it no longer has the right
to do.

The five modes:

    none      no lock. The control, and it oversells immediately.
    redis     a textbook single-node lock, written hygienically. It STILL
              oversells, and it is important that it is written well: the bug
              is not sloppy lock code, it is the lease.
    redlock   the quorum. Its safety argument assumes bounded pauses; the
              episode's own forensic pause violates that. What is MEASURED here
              instead is the crash-restart: a node with no persistence that
              restarts has forgotten what it granted, and the quorum hands out
              a lock somebody already holds.
    advisory  pg_advisory_xact_lock. No TTL to expire, because it is owned by a
              session and dies with it. The first honest answer in the episode.
    fenced    the lock is STILL lost -- nothing here fixes that -- and the
              storage layer refuses the stale write anyway.

Nothing in this file sleeps to make a point. Every expiry is a real TTL running
out while real work is in flight.
"""
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from . import config

# Released with a compare-and-delete, never a bare DEL. A bare DEL lets a
# worker whose lease already expired delete the lock a DIFFERENT worker now
# holds, which is a second bug on top of the one this episode is about. The
# Caching series shipped this Lua and the episode does not re-teach it; it is
# here so that the lock being demonstrated is a GOOD one.
RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


@dataclass
class Held:
    """What a worker believes it holds.

    `token` is the fencing token: strictly increasing per key, minted at
    acquisition. `believed_until` is when the lease was due to expire, and the
    word `believed` is doing real work -- it is the holder's opinion, and the
    holder is the last to find out it is wrong.
    """

    key: str
    owner: str
    token: int = 0
    acquired_at: float = field(default_factory=time.monotonic)
    ttl_ms: int = 0
    # Set when the worker checks, AFTER its work, whether the lease survived.
    # This is the episode's headline mechanism and it is recorded per request
    # rather than inferred from the oversell count.
    lease_expired: bool = False
    # True when the lock was still ours at release time. A hygienic release
    # tells you this for free, and almost nobody looks at it.
    released_cleanly: bool = False

    @property
    def believed_until(self) -> float:
        return self.acquired_at + self.ttl_ms / 1000

    def held_for_ms(self) -> float:
        return (time.monotonic() - self.acquired_at) * 1000


class LockUnavailable(Exception):
    """Somebody else holds it and we are not prepared to wait any longer."""


# ── none ─────────────────────────────────────────────────────────────────────
class NoLock:
    """The control. Two workers, one unit, two parcels.

    It exists so the episode can show that the critical section genuinely needs
    protecting before arguing about which protection to use.
    """

    name = "none"

    async def acquire(self, key: str, owner: str) -> Held:
        return Held(key=key, owner=owner, token=0, ttl_ms=0)

    async def still_held(self, h: Held) -> bool:
        return False

    async def release(self, h: Held) -> None:
        return None


# ── redis ────────────────────────────────────────────────────────────────────
class RedisLock:
    """SET key owner NX PX ttl, released with the Lua compare-and-delete.

    This is the lock most systems actually run, written the way the good blog
    posts tell you to write it. It is not a straw man and the episode says so
    out loud. It oversells anyway.
    """

    name = "redis"

    def __init__(self, url: str) -> None:
        self._r = aioredis.from_url(url, decode_responses=True)

    async def _mint(self, key: str) -> int:
        """A strictly increasing token per key.

        INCR is the whole implementation. It matters that the token comes from
        the same place the lock does and that it is minted at ACQUISITION: a
        token handed out later, or derived from a clock, would not be
        monotonic across the exact case the episode cares about.
        """
        return int(await self._r.incr(f"fence:{key}"))

    async def acquire(self, key: str, owner: str) -> Held:
        ttl = config.lock_ttl_ms()
        deadline = time.monotonic() + config.LOCK_WAIT_SECONDS
        while True:
            ok = await self._r.set(f"lock:{key}", owner, nx=True, px=ttl)
            if ok:
                return Held(key=key, owner=owner, token=await self._mint(key), ttl_ms=ttl)
            if time.monotonic() >= deadline:
                raise LockUnavailable(key)
            await _sleep_poll()

    async def still_held(self, h: Held) -> bool:
        """Ask Redis whether the lock we think we hold is still ours.

        The application under test does NOT call this before writing -- that is
        the point, and no real application does either. The capture calls it so
        the episode can count how often the lease had already gone, and put a
        measured number next to a mechanism instead of asserting one.
        """
        return await self._r.get(f"lock:{h.key}") == h.owner

    async def release(self, h: Held) -> None:
        freed = await self._r.eval(RELEASE_LUA, 1, f"lock:{h.key}", h.owner)
        h.released_cleanly = bool(freed)


# ── redlock ──────────────────────────────────────────────────────────────────
class Redlock:
    """The quorum algorithm, implemented straight, over N independent nodes.

    Acquire on a majority within the validity window or release everything and
    fail. That is the algorithm and it is implemented honestly here.

    What this episode measures is not a flaw in the arithmetic. It is the
    assumption underneath it: these nodes are independent, so a node that
    restarts having forgotten what it granted turns a majority into a majority
    that is WRONG. With persistence off -- see docker-compose.yml -- restarting
    one node is all it takes, and the capture does exactly that.

    antirez's rebuttal is fair and the episode gives it: delayed restarts fix
    this, and Redlock never claimed to survive a node lying about its state.
    The point is that "just run three Redises" is not the free upgrade it looks
    like.
    """

    name = "redlock"

    def __init__(self, urls: list[str]) -> None:
        self._nodes = [aioredis.from_url(u, decode_responses=True) for u in urls]
        self._quorum = len(self._nodes) // 2 + 1

    async def _mint(self, key: str) -> int:
        # Minted on the first node that answers. A token from a quorum member
        # that later forgets is exactly the token the fencing act refuses.
        for n in self._nodes:
            try:
                return int(await n.incr(f"fence:{key}"))
            except Exception:
                continue
        return 0

    async def acquire(self, key: str, owner: str) -> Held:
        ttl = config.lock_ttl_ms()
        deadline = time.monotonic() + config.LOCK_WAIT_SECONDS
        while True:
            started = time.monotonic()
            got = []
            for n in self._nodes:
                try:
                    if await n.set(f"lock:{key}", owner, nx=True, px=ttl):
                        got.append(n)
                except Exception:
                    # A node being down is an ordinary case for this algorithm.
                    continue
            elapsed_ms = (time.monotonic() - started) * 1000
            # The validity check the algorithm requires: the lock is only worth
            # having if enough of the lease is left to do the work in.
            if len(got) >= self._quorum and elapsed_ms < ttl:
                return Held(key=key, owner=owner, token=await self._mint(key), ttl_ms=ttl)
            for n in got:
                try:
                    await n.eval(RELEASE_LUA, 1, f"lock:{key}", owner)
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                raise LockUnavailable(key)
            await _sleep_poll()

    async def still_held(self, h: Held) -> bool:
        alive = 0
        for n in self._nodes:
            try:
                if await n.get(f"lock:{h.key}") == h.owner:
                    alive += 1
            except Exception:
                continue
        return alive >= self._quorum

    async def release(self, h: Held) -> None:
        freed = 0
        for n in self._nodes:
            try:
                freed += int(await n.eval(RELEASE_LUA, 1, f"lock:{h.key}", h.owner) or 0)
            except Exception:
                continue
        h.released_cleanly = freed >= self._quorum


# ── advisory ─────────────────────────────────────────────────────────────────
class AdvisoryLock:
    """pg_advisory_xact_lock, taken inside the transaction that does the work.

    The first genuinely different safety story in the episode: there is no TTL
    to expire. The lock is owned by a session and it dies with the session, so
    a worker that is paused, swapped out or garbage-collecting for forty
    seconds still holds it when it wakes up. Nothing can be handed out
    underneath it.

    The caveats are the ones that actually bite, and the episode says all three:

      * a transaction-mode connection pooler silently breaks session-scoped
        advisory locks, because the next statement may land on another backend.
        The `_xact_` variant is the one to reach for, and it is what is used
        here.
      * a half-open connection holds the lock until the kernel's keepalive
        notices, which can be a very long time.
      * these live in the normal lock manager, so they appear in pg_locks and
        they PARTICIPATE IN DEADLOCK DETECTION -- which is Episode 3 arriving
        again, by a different road.

    It is acquired by the scenario itself rather than here, because it has to
    be taken on the same connection and inside the same transaction as the
    write. That is not an implementation detail; it is the reason it is safe.
    """

    name = "advisory"

    def __init__(self, pg) -> None:
        self._pg = pg

    async def acquire(self, key: str, owner: str) -> Held:
        # No TTL, so nothing to believe about an expiry. The token is still
        # minted so the fenced comparison is like for like.
        return Held(key=key, owner=owner, token=0, ttl_ms=0)

    async def still_held(self, h: Held) -> bool:
        # It cannot have expired. That is the entire selling point.
        return True

    async def release(self, h: Held) -> None:
        h.released_cleanly = True


async def _sleep_poll() -> None:
    import asyncio

    await asyncio.sleep(config.LOCK_POLL_SECONDS)


def build(kind: str, pg=None):
    if kind == "none":
        return NoLock()
    if kind in ("redis", "fenced"):
        # `fenced` uses the SAME lock as `redis`, deliberately. Nothing about
        # the locking is improved; the difference is entirely in what the
        # storage layer does with the token when the write arrives.
        return RedisLock(config.REDIS_URL)
    if kind == "redlock":
        return Redlock(config.REDLOCK_URLS)
    if kind == "advisory":
        return AdvisoryLock(pg)
    raise ValueError(f"unknown lock {kind!r}")
