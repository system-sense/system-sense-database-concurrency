"""Turns the raw capture logs into capture/metrics.json.

One row per cell of the matrix. The engines' error codes are kept as the driver
reported them -- SQLSTATE on Postgres, errno on MySQL -- and deliberately never
normalised into a shared vocabulary, because the episode is about the two of
them disagreeing and flattening the codes would hide the thing being measured.
"""
import json
import pathlib
import re

OUT = pathlib.Path("capture")

CELLS = [
    ("pg-rc-rmw", "postgres", "read-committed", "read_modify_write"),
    ("my-rc-rmw", "mysql", "read-committed", "read_modify_write"),
    ("pg-rr-rmw", "postgres", "repeatable-read", "read_modify_write"),
    ("my-rr-rmw", "mysql", "repeatable-read", "read_modify_write"),
    ("pg-rr-cti", "postgres", "repeatable-read", "count_then_insert"),
    ("my-rr-cti", "mysql", "repeatable-read", "count_then_insert"),
    ("pg-ser-rmw", "postgres", "serializable", "read_modify_write"),
    ("my-ser-rmw", "mysql", "serializable", "read_modify_write"),
    ("pg-rc-guard", "postgres", "read-committed", "where_guard"),
    ("my-rc-guard", "mysql", "read-committed", "where_guard"),
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


def cell(tag: str, engine: str, isolation: str, scenario: str) -> dict:
    body = text(f"cell-{tag}.log")
    driver = kv("DRIVER", body)
    state = blob(f"STATE {tag}", body).get(engine, {})
    st = blob(f"STATS {tag}", body)
    statuses = st.get("statuses", {})

    stock_before = 100
    units_sold = state.get("units_sold", 0)
    reservations = state.get("reservations", 0)
    # The count_then_insert scenario books reservations rather than orders, so
    # the unit that oversells is different. Same question either way: how many
    # seats does the system believe it has sold, against how many existed.
    booked = reservations if scenario == "count_then_insert" else units_sold

    return {
        "tag": tag,
        "engine": engine,
        "isolation": isolation,
        "scenario": scenario,
        "confirmed": statuses.get("confirmed", 0),
        "sold_out": statuses.get("sold_out", 0),
        "lost_race": statuses.get("lost_race", 0),
        "aborted": statuses.get("aborted", 0),
        "check_violation": statuses.get("check_violation", 0),
        "errors": statuses.get("error", 0),
        # SQLSTATE on Postgres, errno on MySQL. Never normalised.
        "codes": st.get("codes", {}),
        "stock_before": stock_before,
        "stock_after": state.get("stock", 0),
        "orders": state.get("orders", 0),
        "units_sold": units_sold,
        "reservations": reservations,
        "booked": booked,
        "oversold_units": max(0, booked - stock_before),
        "p50_ms": driver.get("p50_ms", 0),
        "p99_ms": driver.get("p99_ms", 0),
        "p50_confirmed_ms": driver.get("p50_confirmed_ms", 0),
        "p99_confirmed_ms": driver.get("p99_confirmed_ms", 0),
        "orders_per_sec": driver.get("orders_per_sec", 0),
        "window": st.get("window", {}),
        # Only the WHERE-guard retries. Every other cell leaves these at zero,
        # which is the point: the levels hand the application an exception and
        # no loop to catch it in.
        "retries": st.get("retries", {}),
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

    cells = [cell(*c) for c in CELLS]
    by = {c["tag"]: c for c in cells}

    metrics = {
        "scenario": {
            "sku_stock": 100,
            "orders_fired": cells[0]["confirmed"] + cells[0]["sold_out"] + cells[0]["aborted"]
            + cells[0]["lost_race"] + cells[0]["errors"],
            "concurrency": 25,
            "postgres_version": versions.get("postgres", ""),
            "mysql_version": versions.get("mysql", ""),
        },
        "cells": cells,
    }
    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    hdr = f"  {'cell':<13}{'engine':<10}{'isolation':<17}{'booked':>7}{'oversold':>10}{'aborted':>9}  codes"
    print(f"\n{hdr}")
    for c in cells:
        codes = " ".join(f"{k}x{v}" for k, v in sorted(c["codes"].items())) or "-"
        print(f"  {c['tag']:<13}{c['engine']:<10}{c['isolation']:<17}"
              f"{c['booked']:>7}{c['oversold_units']:>10}{c['aborted']:>9}  {codes}")

    guard = [c for c in cells if c["scenario"] == "where_guard"]
    if any(c["retries"] for c in guard):
        print("\n  THE PORTABLE FIX, WITH THE RETRY LOOP IT NEEDS")
        for c in guard:
            r = c["retries"]
            print(f"    {c['engine']:<9} booked {c['booked']:>3}   oversold {c['oversold_units']:>3}"
                  f"   gave up {r.get('abandoned', 0):>3}   retries {r.get('total', 0):>4}"
                  f"   worst order {r.get('max_one_order', 0)} attempts")

    print("\n  THE TWO ROWS THE EPISODE RESTS ON")
    for a, b, what in [("pg-rr-rmw", "my-rr-rmw", "read-modify-write at REPEATABLE READ"),
                       ("pg-rr-cti", "my-rr-cti", "count-then-insert at REPEATABLE READ")]:
        p, m_ = by[a], by[b]
        print(f"  {what}")
        print(f"    postgres  oversold {p['oversold_units']:>3}   aborted {p['aborted']:>3}")
        print(f"    mysql     oversold {m_['oversold_units']:>3}   aborted {m_['aborted']:>3}")


if __name__ == "__main__":
    main()
