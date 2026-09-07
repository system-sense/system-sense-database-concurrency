"""Configuration. Nothing here is the point of the episode; it is all plumbing."""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://sysense:sysense@localhost:5432/sysense")
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "sysense")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "sysense")
MYSQL_DB = os.getenv("MYSQL_DB", "sysense")

PRICING_URL = os.getenv("PRICING_URL", "http://localhost:9000")
PRICING_TIMEOUT_SECONDS = float(os.getenv("PRICING_TIMEOUT_SECONDS", "30"))

# ── Episode 4 ────────────────────────────────────────────────────────────────
#  Fulfilment is not another pricing call. Pricing is a QUESTION -- ask it
#  twice and nothing happened. Fulfilment is a DECISION somebody else acts on,
#  and once it returns, a parcel is on a van. Nothing in this series recalls it.
FULFILMENT_URL = os.getenv("FULFILMENT_URL", "http://localhost:9100")
FULFILMENT_TIMEOUT_SECONDS = float(os.getenv("FULFILMENT_TIMEOUT_SECONDS", "30"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDLOCK_URLS = [u for u in os.getenv(
    "REDLOCK_URLS",
    "redis://localhost:6381,redis://localhost:6382,redis://localhost:6383",
).split(",") if u]

#  How long a worker will keep trying to take the lock before giving up and
#  reporting a real failure. Bounded on purpose: without it the losers simply
#  outlive the load generator and every number becomes about patience.
LOCK_WAIT_SECONDS = float(os.getenv("LOCK_WAIT_SECONDS", "8"))
LOCK_POLL_SECONDS = float(os.getenv("LOCK_POLL_SECONDS", "0.05"))

# ─────────────────────────────────────────────────────────────────────────────
#  EPISODE 4's KNOB.
#
#  none      no lock at all. The control.
#  redis     a hygienic single-node lock: SET NX PX, Lua compare-and-delete.
#            It is written WELL and it still oversells, which is the episode.
#  redlock   the quorum, and the node restart that forgets what it granted.
#  advisory  pg_advisory_xact_lock -- no TTL to expire, because it dies with
#            the session that owns it.
#  fenced    the same lost lock as `redis`, and a storage layer that refuses
#            the stale write anyway.
# ─────────────────────────────────────────────────────────────────────────────
LOCKS = ("none", "redis", "redlock", "advisory", "fenced")

#  THE ONE KNOB.
#
#  The lease. Default 1000 ms against a critical section that spans 400-1599 ms,
#  so some workers finish inside their lease and some do not -- decided by
#  arithmetic on the ids, not by a sleep tuned until the demo worked.
#
#  The hide-the-bug exercise is to raise this above the measured p99. The
#  oversell vanishes and NOTHING HAS BEEN FIXED: the window is narrower, a
#  forty-second GC pause makes it wide again, and not one line of the
#  application changed.
_lock_ttl_ms = int(os.getenv("LOCK_TTL_MS", "1000"))


def lock_ttl_ms() -> int:
    return _lock_ttl_ms


def set_lock_ttl_ms(n: int) -> None:
    """Runtime-settable so one run measures every cell against one build."""
    global _lock_ttl_ms
    _lock_ttl_ms = max(1, int(n))

# The WHERE-guard is Episode 1's optimistic mode, and Episode 1 gave it five
# attempts. It keeps that number so the two episodes can be read against each
# other: without a retry loop a lost race is a refused customer, which measures
# the contention rather than the fix.
MAX_GUARD_RETRIES = int(os.getenv("MAX_GUARD_RETRIES", "5"))

ENGINES = ("postgres", "mysql")

# ─────────────────────────────────────────────────────────────────────────────
#  THE KNOB.
#
#  read-committed   the Episode 1 control. Both engines oversell.
#  repeatable-read  the setting whose name sounds like a promise.
#  serializable     safe on both, and both make you pay in a different currency.
#
#  The ENGINE is the capture's axis rather than the viewer's: the whole episode
#  is the two of them side by side, so a run that only sees one proves nothing.
# ─────────────────────────────────────────────────────────────────────────────
ISOLATIONS = ("read-committed", "repeatable-read", "serializable")

#  read_modify_write   read the stock, subtract in Python, write it back
#  count_then_insert   count the reservations, decide there is room, insert one
#  where_guard         the portable fix: put the value you read in the WHERE
#  basket_checkout     Episode 3: Episode 1's atomic decrement, once per basket
#                      line, in one transaction. Nothing about it is wrong.
#  basket_one_stmt     the trap fix: "just do it in one statement"
SCENARIOS = (
    "read_modify_write",
    "count_then_insert",
    "where_guard",
    "basket_checkout",
    "basket_one_stmt",
    "basket_values_join",
    #  Episode 4: read the stock, decide to allocate the last unit, DISPATCH A
    #  PARCEL, then write. The critical section spans an external side effect,
    #  which is why a row lock is not available here.
    "allocate_and_dispatch",
)

# ─────────────────────────────────────────────────────────────────────────────
#  EPISODE 3's KNOB.
#
#  basket  lock the rows in the order the customer's basket happened to be in
#  sorted  lock them in a total order every transaction agrees on
#
#  That is the whole fix, and it is one word in the handler. There is no new
#  lock, no new table and no new service.
# ─────────────────────────────────────────────────────────────────────────────
LOCK_ORDERS = ("basket", "sorted")

#  Deadlocks cannot be eliminated, only made rare, so a retry is required even
#  once the order is fixed. Zero by default because Episode 3 has to show the
#  unhandled case first: most applications have no 40P01 handler at all, and
#  that is why the order is simply lost.
_retries = int(os.getenv("DEADLOCK_RETRIES", "0"))


def retries() -> int:
    return _retries


def set_retries(n: int) -> None:
    """Runtime-settable so the capture can measure the unhandled case and the
    retried case in one run, against one build, rather than restarting the
    stack between them and comparing two different sets of container starts."""
    global _retries
    _retries = max(0, int(n))

#  The hide-the-bug exercise. A one-item basket holds exactly one lock, and a
#  transaction holding one lock cannot be half of a cycle. The count goes to
#  zero and not one line of the bug has been fixed.
MAX_BASKET_ITEMS = int(os.getenv("MAX_BASKET_ITEMS", "3"))

#  Postgres only, and 0 means "off".
#
#  A blocked transaction waits for deadlock_timeout before anything even LOOKS
#  for a cycle. lock_timeout gives up first: the transaction fails in whatever
#  you set here instead of stalling for a second and then being chosen as the
#  victim. Failing fast is not a fix for the order, but it is the difference
#  between a slow outage and a fast error.
#  How long one order may spend retrying before it gives up and reports a real
#  failure. Without a budget the retries simply outlive the load generator.
RETRY_BUDGET_SECONDS = float(os.getenv("RETRY_BUDGET_SECONDS", "10"))

_lock_timeout_ms = int(os.getenv("LOCK_TIMEOUT_MS", "0"))


def lock_timeout_ms() -> int:
    return _lock_timeout_ms


def set_lock_timeout_ms(n: int) -> None:
    global _lock_timeout_ms
    _lock_timeout_ms = max(0, int(n))

_current = {
    "engine": os.getenv("ENGINE", "postgres"),
    "isolation": os.getenv("ISOLATION", "read-committed"),
    "scenario": os.getenv("SCENARIO", "read_modify_write"),
    "lock_order": os.getenv("LOCK_ORDER", "basket"),
    "lock": os.getenv("LOCK", "none"),
}


def get(key: str) -> str:
    return _current[key]


def set_all(**kw: str) -> None:
    for k, v in kw.items():
        if v is None:
            continue
        allowed = {
            "engine": ENGINES,
            "isolation": ISOLATIONS,
            "scenario": SCENARIOS,
            "lock_order": LOCK_ORDERS,
            "lock": LOCKS,
        }[k]
        if v not in allowed:
            raise ValueError(f"unknown {k} {v!r}; expected one of {allowed}")
        _current[k] = v
