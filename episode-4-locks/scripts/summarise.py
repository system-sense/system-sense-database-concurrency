"""Turns the raw capture logs into capture/metrics.json.

One row per cell. The engines' error codes are kept as the driver reported them
-- SQLSTATE on Postgres, errno on MySQL -- and deliberately never normalised,
because how each engine reports a cycle is part of what is being measured.

The number this episode turns on is NOT an oversell. A deadlock victim is rolled
back, so the stock is exactly right; what is wrong is that the order is gone.
So `lost_orders` and `stock_left` are read together: a full shelf and a turned
away customer.
"""
import json
import pathlib
import re

OUT = pathlib.Path("capture")

CELLS = [
    #  tag                  engine      order     scenario           retries items lock_to
    ("pg-basket",           "postgres", "basket", "basket_checkout", 0, 3, 0),
    ("my-basket",           "mysql",    "basket", "basket_checkout", 0, 3, 0),
    ("pg-sorted",           "postgres", "sorted", "basket_checkout", 0, 3, 0),
    ("my-sorted",           "mysql",    "sorted", "basket_checkout", 0, 3, 0),
    ("pg-basket-retry",     "postgres", "basket", "basket_checkout", 3, 3, 0),
    ("pg-basket-locktimeout", "postgres", "basket", "basket_checkout", 0, 3, 50),
    ("pg-sorted-retry",     "postgres", "sorted", "basket_checkout", 3, 3, 0),
    ("pg-one-stmt",         "postgres", "basket", "basket_one_stmt", 0, 3, 0),
    ("pg-values-join",      "postgres", "basket", "basket_values_join", 0, 3, 0),
    ("pg-basket-single",    "postgres", "basket", "basket_checkout", 0, 1, 0),
]


def text(name: str) -> str:
    p = OUT / name
    return p.read_text() if p.exists() else ""


def kv(prefix: str, body: str) -> dict:
    found: dict = {}
    for line in body.splitlines():
        if line.startswith(prefix):
            found = {}
            for k, v in re.findall(r"(\w+)=(-?[\d.]+)", line):
                found[k] = float(v) if "." in v else int(v)
    return found


def blob(prefix: str, body: str) -> dict:
    for line in body.splitlines():
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):].strip())
            except json.JSONDecodeError:
                return {}
    return {}


def cell(tag, engine, order, scenario, retries, items, lock_to) -> dict:
    body = text(f"cell-{tag}.log")
    driver = kv("DRIVER", body)
    state = blob(f"STATE {tag}", body).get(engine, {})
    st = blob(f"STATS {tag}", body)
    statuses = st.get("statuses", {})

    confirmed = statuses.get("confirmed", 0)
    deadlocked = statuses.get("deadlocked", 0)
    timed_out = statuses.get("lock_timeout", 0)

    return {
        "tag": tag,
        "engine": engine,
        "lock_order": order,
        "scenario": scenario,
        "retries_configured": retries,
        "basket_items": items,
        "lock_timeout_ms": lock_to,
        "confirmed": confirmed,
        "sold_out": statuses.get("sold_out", 0),
        "deadlocked": deadlocked,
        "lock_timeouts": timed_out,
        "errors": statuses.get("error", 0),
        # The order was never placed and nobody handled the exception. Same unit
        # as the rest of the series, opposite sign to Episode 1's oversell.
        "lost_orders": deadlocked + timed_out,
        # SQLSTATE on Postgres, errno on MySQL. Never normalised.
        "codes": st.get("codes", {}),
        "stock_left": state.get("stock_total", 0),
        "orders": state.get("orders", 0),
        "items_sold": state.get("items_sold", 0),
        "retries_used": st.get("retries", {}).get("total", 0) if isinstance(st.get("retries"), dict) else 0,
        # How long a doomed order was ALIVE before the database gave up on it:
        # transaction open to driver raise, so it includes the pricing call and
        # any time spent queued behind other blocked transactions. It is not
        # "detection latency" and is deliberately not called that.
        "doomed_lifetime": st.get("deadlock_wait", {}),
        "p50_ms": driver.get("p50_ms", 0),
        "p99_ms": driver.get("p99_ms", 0),
        "p50_confirmed_ms": driver.get("p50_confirmed_ms", 0),
        "p99_confirmed_ms": driver.get("p99_confirmed_ms", 0),
        "orders_per_sec": driver.get("orders_per_sec", 0),
    }


def main() -> None:
    up = text("01-compose-up.log")
    versions = {}
    m = re.search(r'\{"postgres":.*?\}', up)
    if m:
        try:
            versions = json.loads(m.group(0))
        except json.JSONDecodeError:
            versions = {}

    settings = text("02-settings.log")

    cells = [cell(*c) for c in CELLS]
    by = {c["tag"]: c for c in cells}

    metrics = {
        "scenario": {
            "skus": 8,
            "stock_per_sku": 100,
            "stock_total": 800,
            "orders_fired": 300,
            "concurrency": 25,
            "basket_items_max": 3,
            "postgres_version": versions.get("postgres", ""),
            "mysql_version": versions.get("mysql", ""),
            # Read off the engine, not off the documentation.
            "deadlock_timeout": next((l.strip() for l in settings.splitlines()
                                      if re.fullmatch(r"\s*\d+\w*s?\s*", l)), ""),
        },
        "cells": cells,
    }
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    hdr = (f"  {'cell':<18}{'engine':<10}{'order':<8}{'confirmed':>10}{'deadlocked':>12}"
           f"{'stock left':>12}{'alive p50':>12}{'p99 order':>11}  codes")
    print(f"\n{hdr}")
    for c in cells:
        codes = " ".join(f"{k}x{v}" for k, v in sorted(c["codes"].items())) or "-"
        d = c["doomed_lifetime"].get("median_ms", 0)
        print(f"  {c['tag']:<18}{c['engine']:<10}{c['lock_order']:<8}"
              f"{c['confirmed']:>10}{c['deadlocked']:>12}{c['stock_left']:>12}"
              f"{d:>11.0f}m{c['p99_ms']:>10}  {codes}")

    print("\n  THE NUMBER THAT INVERTS")
    b = by.get("pg-basket", {})
    print(f"    {b.get('lost_orders', 0)} customers turned away, and "
          f"{b.get('stock_left', 0)} units still on the shelves")

    print("\n  HOW LONG A DOOMED ORDER STAYED ALIVE (not detection latency)")
    for t in ("pg-basket", "my-basket"):
        c = by.get(t, {})
        w = c.get("doomed_lifetime", {})
        print(f"    {c.get('engine','?'):<9} median {w.get('median_ms', 0):>8} ms   "
              f"p99 {w.get('p99_ms', 0):>8} ms   over {w.get('count', 0)} deadlocks")

    print("\n  THE ONE-WORD FIX")
    for t in ("pg-basket", "pg-sorted"):
        c = by.get(t, {})
        print(f"    {c.get('lock_order','?'):<7} deadlocks {c.get('deadlocked',0):>4}   "
              f"confirmed {c.get('confirmed',0):>4}   {c.get('orders_per_sec',0):>7} orders/sec")


if __name__ == "__main__":
    main()
