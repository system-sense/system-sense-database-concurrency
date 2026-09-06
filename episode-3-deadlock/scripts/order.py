#!/usr/bin/env python3
"""The customers. Three hundred of them, pressing Buy at the same moment.

    python3 scripts/order.py --orders 300 --concurrency 25

Nothing here is a straw man. Every customer places one honest order for one
unit. There are no retries and no duplicates: this is not the last series. Three
hundred different people are each entitled to exactly one seat, and the only
question is whether the shop can count.

Standard library only, on purpose: this must be readable by someone who has
never seen the repository before.
"""
import argparse
import json
import random
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

APP_URL = "http://localhost:8000/api/orders"


def basket_for(customer_id: int, skus: int, max_items: int) -> list[int]:
    """The customer's basket, in the order they built it.

    Deterministic by customer id, so a re-run is comparable, and NOT sorted:
    the whole episode is that nobody chose this order. Customer 7 and customer
    12 can easily end up with the same two SKUs in opposite orders, which is
    exactly the pair that deadlocks.
    """
    rnd = random.Random(customer_id * 7919)
    # One item is still one item OUT OF THE SAME HOT SET. Sending every
    # single-item order to SKU 1 would show zero deadlocks for the wrong reason
    # -- one contended row rather than one lock per transaction -- and would not
    # be the same load as the basket cells it is being compared against.
    n = rnd.randint(2, max_items) if max_items > 1 else 1
    picked = rnd.sample(range(1, skus + 1), n)
    rnd.shuffle(picked)
    return picked


def place(customer_id: int, sku_id: int, qty: int, basket: list[int]) -> tuple[str, float]:
    payload = json.dumps(
        {"sku_id": sku_id, "customer_id": customer_id, "qty": qty, "basket": basket}
    ).encode()
    req = urllib.request.Request(
        APP_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
        return body.get("status", "confirmed"), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
            status = body.get("status", f"http_{e.code}")
        except Exception:
            status = f"http_{e.code}"
        return status, (time.perf_counter() - started) * 1000
    except Exception as e:  # noqa: BLE001
        return f"error_{type(e).__name__}", (time.perf_counter() - started) * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=300)
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--sku", type=int, default=1)
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--label", default="run")
    # Episode 3. One item per order cannot deadlock: a transaction holding one
    # lock is never half of a cycle. That is the hide-the-bug exercise.
    ap.add_argument("--max-basket-items", type=int, default=1)
    ap.add_argument("--skus", type=int, default=8)
    args = ap.parse_args()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(
            pool.map(
                lambda cid: place(
                    cid, args.sku, args.qty,
                    basket_for(cid, args.skus, args.max_basket_items),
                ),
                range(1, args.orders + 1),
            )
        )
    wall = time.perf_counter() - started

    counts: dict[str, int] = {}
    for status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    lat = sorted(ms for _, ms in results)
    # Confirmed orders only. A rejection is fast by construction -- a sold-out
    # check returns before the pricing call -- so a percentile over everything
    # measures how quickly the shop can say no, which is not what any of these
    # modes are being compared on. `pessimistic` was the one this flattered:
    # 200 of its 300 requests never touched the locked row at all.
    sold = sorted(ms for status, ms in results if status == "confirmed")

    def pct(series: list[float], p: float) -> float:
        return series[min(len(series) - 1, int(len(series) * p))] if series else 0.0

    for status, n in sorted(counts.items()):
        print(f"  {status:<16} {n}")

    print(
        f"DRIVER label={args.label} orders_fired={args.orders} "
        f"concurrency={args.concurrency} qty_per_order={args.qty} "
        f"confirmed={counts.get('confirmed', 0)} "
        f"sold_out={counts.get('sold_out', 0)} "
        f"deadlocked={counts.get('deadlocked', 0)} "
        f"conflict={counts.get('conflict', 0)} "
        f"check_violation={counts.get('check_violation', 0)} "
        f"other_errors={sum(n for s, n in counts.items() if s.startswith(('error_', 'http_')))} "
        f"p50_ms={int(statistics.median(lat))} p99_ms={int(pct(lat, 0.99))} "
        f"p50_confirmed_ms={int(pct(sold, 0.5))} p99_confirmed_ms={int(pct(sold, 0.99))} "
        f"wall_ms={int(wall * 1000)} orders_per_sec={round(args.orders / wall, 1)}",
        flush=True,
    )


if __name__ == "__main__":
    main()
