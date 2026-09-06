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
SCENARIOS = ("read_modify_write", "count_then_insert", "where_guard")

_current = {
    "engine": os.getenv("ENGINE", "postgres"),
    "isolation": os.getenv("ISOLATION", "read-committed"),
    "scenario": os.getenv("SCENARIO", "read_modify_write"),
}


def get(key: str) -> str:
    return _current[key]


def set_all(**kw: str) -> None:
    for k, v in kw.items():
        if v is None:
            continue
        allowed = {"engine": ENGINES, "isolation": ISOLATIONS, "scenario": SCENARIOS}[k]
        if v not in allowed:
            raise ValueError(f"unknown {k} {v!r}; expected one of {allowed}")
        _current[k] = v
