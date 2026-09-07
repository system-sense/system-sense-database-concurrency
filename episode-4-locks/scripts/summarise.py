"""Turns the raw capture logs into capture/metrics.json.

One row per cell. Every figure the episode quotes comes from the FLEET cells;
the forensic pause and the Redlock restart write their own logs and appear here
only as evidence that they ran.

The number this episode turns on is a PARCEL, not a row. A worker whose lease
expired mid-dispatch has already handed a consignment to fulfilment, and no
amount of rolling back reaches it. So the arithmetic that matters is:

    parcels_without_sale = parcels_dispatched - units_the_shelf_gave_up

Under `none` and `redis` that gap is a genuine oversell: two parcels went out
and the shelf was decremented once. Under `fenced` the gap is the episode's
closing line instead -- the storage layer REFUSED the stale write, which is why
the shelf is right, and the parcel had already gone anyway. The arithmetic is
identical and the meaning is not, so `fenced_out` is carried beside it rather
than folded into it.
"""
import json
import pathlib
import re

OUT = pathlib.Path("capture")

#  tag                mode        ttl_ms
CELLS = [
    ("none",             "none",     1000),
    ("redis",            "redis",    1000),
    ("redlock",          "redlock",  1000),
    ("advisory",         "advisory", 1000),
    ("fenced",           "fenced",   1000),
    ("redis-long-lease", "redis",    2600),
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


def cell(tag: str, mode: str, ttl: int, stock_per_sku: int, skus: int) -> dict:
    body = text(f"cell-{tag}.log")
    driver = kv("DRIVER", body)
    header = kv(f"CELL {tag}", body)
    state = blob(f"STATE {tag}", body).get("postgres", {})
    st = blob(f"STATS {tag}", body)
    lock = st.get("lock", {})
    statuses = st.get("statuses", {})

    started_total = (header.get("stock_per_sku", stock_per_sku)) * skus
    stock_left = state.get("stock_total", 0)
    units_sold = started_total - stock_left
    parcels = lock.get("dispatched", 0)

    return {
        "tag": tag,
        "lock": mode,
        "lease_ms": lock.get("ttl_ms", ttl),
        "stock_total": started_total,
        "stock_left": stock_left,
        # What the shelf says left the shelf.
        "units_sold": units_sold,
        # What fulfilment actually put on a van. Counted when the call returns,
        # not when the write succeeds, because that is when it stopped being
        # reversible.
        "parcels": parcels,
        # The episode's headline. See the module docstring: same arithmetic,
        # two different meanings, and `fenced_out` is what tells them apart.
        "parcels_without_sale": parcels - units_sold,
        # The mechanism, counted directly rather than inferred from the gap.
        "lease_expired": lock.get("lease_expired", 0),
        # The storage layer refusing a writer whose lock had already gone.
        "fenced_out": lock.get("fenced_out", 0),
        "lock_unavailable": lock.get("lock_unavailable", 0),
        # A release that found the lock was no longer ours. The hygienic Lua
        # release is what makes this visible; a bare DEL would have deleted
        # somebody else's lock and said nothing.
        "released_not_ours": lock.get("released_not_ours", 0),
        "confirmed": statuses.get("confirmed", 0),
        "sold_out": statuses.get("sold_out", 0),
        "orders": state.get("orders", 0),
        # How long the critical section actually took. The lease is set against
        # THIS distribution, which is why the expiries are emergent.
        "critical_section": lock.get("critical_section", {}),
        # By how much the workers that overran their lease overran it.
        "lease_overrun": lock.get("lease_overrun", {}),
        # advisory only: what holding a transaction open across an external
        # call costs at the pool.
        "pool_wait": lock.get("pool_wait", {}),
        "fenced_examples": lock.get("fenced_examples", []),
        "p50_ms": driver.get("p50_ms", 0),
        "p99_ms": driver.get("p99_ms", 0),
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
    run = kv("orders=", up)
    stock_per_sku = run.get("stock_per_sku", 5)
    skus = 8

    fulfil = {}
    mf = re.search(r'\{"ok":true,"base_ms":(\d+),"spread_ms":(\d+)\}', text("02-settings.log"))
    if mf:
        fulfil = {"base_ms": int(mf.group(1)), "spread_ms": int(mf.group(2))}

    cells = [cell(t, m_, ttl, stock_per_sku, skus) for t, m_, ttl in CELLS]
    by = {c["tag"]: c for c in cells}

    metrics = {
        "scenario": {
            "skus": skus,
            "stock_per_sku": stock_per_sku,
            "stock_total": stock_per_sku * skus,
            "orders_fired": run.get("orders", 300),
            "concurrency": run.get("concurrency", 25),
            "lease_ms": run.get("lease_ms", 1000),
            "postgres_version": versions.get("postgres", ""),
            "fulfilment_latency": fulfil,
        },
        "cells": cells,
        # Named so nobody can quote them by accident. Both are mechanism.
        "evidence_only": {
            "forensic_pause": "capture/06-forensic-pause.log",
            "redlock_restart": "capture/07-redlock-restart.log",
        },
    }
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    hdr = (f"  {'cell':<18}{'lock':<10}{'lease':>7}{'parcels':>9}{'sold':>7}"
           f"{'NO SALE':>9}{'expired':>9}{'fenced':>8}{'ord/sec':>9}")
    print(f"\n{hdr}")
    for c in cells:
        print(f"  {c['tag']:<18}{c['lock']:<10}{c['lease_ms']:>7}{c['parcels']:>9}"
              f"{c['units_sold']:>7}{c['parcels_without_sale']:>9}{c['lease_expired']:>9}"
              f"{c['fenced_out']:>8}{c['orders_per_sec']:>9}")

    print("\n  THE LEASE IS SHORTER THAN THE WORK")
    r = by.get("redis", {})
    cs = r.get("critical_section", {})
    print(f"    lease {r.get('lease_ms', 0)} ms against a critical section of "
          f"median {cs.get('median_ms', 0)} ms, p95 {cs.get('p95_ms', 0)} ms, "
          f"max {cs.get('max_ms', 0)} ms")
    ov = r.get("lease_overrun", {})
    print(f"    {r.get('lease_expired', 0)} leases expired mid-work, overrunning by "
          f"median {ov.get('median_ms', 0)} ms and up to {ov.get('max_ms', 0)} ms")

    print("\n  A GOOD LOCK IS NOT ENOUGH")
    for t in ("none", "redis", "advisory", "fenced"):
        c = by.get(t, {})
        print(f"    {c.get('lock', '?'):<9} {c.get('parcels', 0):>3} parcels, "
              f"{c.get('units_sold', 0):>3} sold, "
              f"{c.get('parcels_without_sale', 0):>3} without a sale, "
              f"{c.get('fenced_out', 0):>3} refused by the row")

    print("\n  THE HIDE-THE-BUG EXERCISE")
    for t in ("redis", "redis-long-lease"):
        c = by.get(t, {})
        print(f"    lease {c.get('lease_ms', 0):>5} ms -> "
              f"{c.get('lease_expired', 0):>3} expiries, "
              f"{c.get('parcels_without_sale', 0):>3} parcels without a sale")
    print("    Nothing was fixed. The window is narrower.")

    a = by.get("advisory", {})
    pw = a.get("pool_wait", {})
    print("\n  WHAT ADVISORY LOCKS COST")
    print(f"    pool wait median {pw.get('median_ms', 0)} ms, p99 {pw.get('p99_ms', 0)} ms, "
          f"over {pw.get('count', 0)} acquisitions")


if __name__ == "__main__":
    main()
