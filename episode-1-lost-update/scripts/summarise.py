"""Turns the raw capture logs into capture/metrics.json.

Two numbers carry this episode and both come from here:

    units sold     — what the order book says three hundred customers bought.
    stock left     — what the shelf says is still there.

Add them up against the hundred units that existed and the difference is stock
you have already sold and do not have. Nothing in the application notices,
because neither number is wrong on its own.
"""
import json
import pathlib
import re

OUT = pathlib.Path("capture")
MODES = ("naive", "atomic", "pessimistic", "optimistic")


def text(name: str) -> str:
    p = OUT / name
    return p.read_text() if p.exists() else ""


def kv(prefix: str, body: str) -> dict:
    """Parse the last `PREFIX k=v k=v ...` line in a log."""
    found: dict = {}
    for line in body.splitlines():
        if line.startswith(prefix):
            found = {}
            for k, v in re.findall(r"(\w+)=(-?[\d.]+)", line):
                found[k] = float(v) if "." in v else int(v)
    return found


def stats(mode: str) -> dict:
    p = OUT / f"stats-{mode}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def scenario_for(mode: str, log: str) -> dict:
    body = text(log)
    driver = kv("DRIVER", body)
    result = kv(f"RESULT {mode}", body)
    app = stats(mode)

    stock_before = result.get("stock_before", 0)
    stock_after = result.get("stock_after", 0)
    units_sold = result.get("units_sold", 0)

    # What actually left the shelf, against what we told customers they bought.
    left_the_shelf = stock_before - stock_after
    lost_decrements = units_sold - left_the_shelf
    oversold = max(0, units_sold - stock_before)
    # Stock the database believes it still has, all of which is already sold.
    phantom = stock_after if units_sold >= stock_before else 0

    return {
        "mode": mode,
        "orders_fired": driver.get("orders_fired", 0),
        "concurrency": driver.get("concurrency", 0),
        "orders_created": result.get("orders_created", 0),
        "units_sold": units_sold,
        "stock_before": stock_before,
        "stock_after": stock_after,
        "units_left_the_shelf": left_the_shelf,
        "lost_decrements": lost_decrements,
        "oversold_units": oversold,
        "oversold_pct": round(100 * oversold / stock_before, 1) if stock_before else 0.0,
        "phantom_stock": phantom,
        "sold_out_rejections": driver.get("sold_out", 0),
        "conflict_rejections": driver.get("conflict", 0),
        "check_constraint_violations": app.get("check_constraint_violations", 0),
        "other_errors": driver.get("other_errors", 0),
        "retries_total": app.get("retries_total", 0),
        "max_retries_one_order": app.get("max_retries_one_order", 0),
        "abandoned_after_max_retries": app.get("abandoned_after_max_retries", 0),
        "p50_ms": driver.get("p50_ms", 0),
        "p99_ms": driver.get("p99_ms", 0),
        "p50_confirmed_ms": driver.get("p50_confirmed_ms", 0),
        "p99_confirmed_ms": driver.get("p99_confirmed_ms", 0),
        "wall_ms": driver.get("wall_ms", 0),
        "orders_per_sec": driver.get("orders_per_sec", 0),
    }


def main() -> None:
    up = text("01-compose-up.log")
    base, spread = 60, 240
    m = re.search(r"^(\d+) (\d+)\s*$", up, re.M)
    if m:
        base, spread = int(m.group(1)), int(m.group(2))
    conf = kv("orders=", up.replace("orders=", "CONF orders="))

    logs = {"naive": "02-naive.log", "atomic": "03-atomic.log",
            "pessimistic": "04-pessimistic.log", "optimistic": "05-optimistic.log"}
    modes = {m_: scenario_for(m_, logs[m_]) for m_ in MODES}

    orders_fired = modes["naive"]["orders_fired"] or conf.get("orders", 0)
    # The window is a real network call, so its span is knowable exactly:
    # every customer id that placed an order produced one of these.
    lat = [base + (1 * 137 + c * 31) % spread for c in range(1, orders_fired + 1)]

    metrics = {
        "scenario": {
            "sku_id": 1,
            "sku_stock": modes["naive"]["stock_before"] or conf.get("stock", 0),
            "orders_fired": orders_fired,
            "concurrency": modes["naive"]["concurrency"] or conf.get("concurrency", 0),
            "qty_per_order": 1,
            "pricing_base_ms": base,
            "pricing_spread_ms": spread,
            "pricing_min_ms": min(lat) if lat else 0,
            "pricing_max_ms": max(lat) if lat else 0,
            "max_optimistic_retries": int(
                (re.search(r"max optimistic retries = (\d+)", up) or ["", 0])[1]
            ),
        },
        "window": stats("naive").get("window", {}),
        **modes,
    }

    (OUT / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")

    s = metrics["scenario"]
    n, a = metrics["naive"], metrics["atomic"]
    print(f"\n  {s['orders_fired']} orders, {s['concurrency']} at a time, "
          f"{s['sku_stock']} units in stock\n")
    print(f"  {'':<14}{'sold':>6}{'shelf':>7}{'oversold':>10}{'lost':>7}"
          f"{'p99 sale':>10}{'ord/s':>8}")
    for k in MODES:
        r = metrics[k]
        print(f"  {k:<14}{r['units_sold']:>6}{r['stock_after']:>7}"
              f"{r['oversold_units']:>10}{r['lost_decrements']:>7}"
              f"{r['p99_confirmed_ms']:>10}{r['orders_per_sec']:>8}")
    print(f"\n  the CHECK constraint fired {n['check_constraint_violations']} times")
    print(f"  atomic oversold {a['oversold_units']} units")


if __name__ == "__main__":
    main()
