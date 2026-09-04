"""Configuration. Nothing here is the point of the episode; it is all plumbing."""
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgres://sysense:sysense@localhost:5432/sysense")
PRICING_URL = os.getenv("PRICING_URL", "http://localhost:9000")
PRICING_TIMEOUT_SECONDS = float(os.getenv("PRICING_TIMEOUT_SECONDS", "30"))

# How many times the optimistic handler will re-read and try again before it
# gives up on a customer. Five is generous; watch how many orders still run out.
MAX_OPTIMISTIC_RETRIES = int(os.getenv("MAX_OPTIMISTIC_RETRIES", "5"))

# ─────────────────────────────────────────────────────────────────────────────
#  THE KNOB.
#
#  naive        SELECT the stock, subtract in Python, UPDATE the literal back.
#               This is what your ORM writes for `item.stock -= 1`.
#  atomic       UPDATE ... SET stock = stock - $2 WHERE stock >= $2. One
#               statement. The database never lets go between the read and the
#               write, because there is no between.
#  pessimistic  SELECT ... FOR UPDATE, then the same read-modify-write. Correct,
#               and everybody queues behind one row for the whole pricing call.
#  optimistic   A version column and a retry loop. Correct, and not free.
# ─────────────────────────────────────────────────────────────────────────────
MODES = ("naive", "atomic", "pessimistic", "optimistic")

_current = {"mode": os.getenv("ORDER_MODE", "naive")}


def mode() -> str:
    return _current["mode"]


def set_mode(name: str) -> None:
    if name not in MODES:
        raise ValueError(f"unknown mode {name!r}; expected one of {MODES}")
    _current["mode"] = name
