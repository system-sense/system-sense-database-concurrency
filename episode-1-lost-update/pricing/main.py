"""The pricing service. It exists to take a realistic amount of time.

Every checkout in the world calls something between reading a row and writing
it back: a pricing engine, a fraud check, a tax lookup, a loyalty balance. That
call is the race window, and it is why the window in a real system is
milliseconds wide rather than microseconds.

The latency is a DETERMINISTIC function of the ids:

    PRICING_BASE_MS + (sku_id * 137 + customer_id * 31) % PRICING_SPREAD_MS

which matters more than it looks. A random sleep would make the oversell a
different number on every run and an `asyncio.sleep` in the handler would make
it a setting rather than a measurement. This is neither: it is a real network
call whose duration varies per customer the way a real one does, so the number
the episode quotes is emergent and reproduces on your machine.
"""
import asyncio
import os

from fastapi import FastAPI

BASE_MS = int(os.getenv("PRICING_BASE_MS", "60"))
SPREAD_MS = int(os.getenv("PRICING_SPREAD_MS", "240"))

app = FastAPI(title="System Sense — pricing")


def latency_ms(sku_id: int, customer_id: int) -> int:
    return BASE_MS + (sku_id * 137 + customer_id * 31) % SPREAD_MS


@app.get("/health")
async def health():
    return {"ok": True, "base_ms": BASE_MS, "spread_ms": SPREAD_MS}


@app.get("/price")
async def price(sku_id: int, customer_id: int, qty: int = 1):
    took = latency_ms(sku_id, customer_id)
    await asyncio.sleep(took / 1000)
    return {"sku_id": sku_id, "unit_cents": 4000, "line_cents": 4000 * qty,
            "took_ms": took}
