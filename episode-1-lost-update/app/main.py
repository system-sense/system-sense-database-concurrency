"""System Sense — DB Concurrency Ep.1: the checkout that sells stock twice.

Run it:      docker compose up --build
Try it:      curl -s -X POST localhost:8000/api/orders \
               -H 'content-type: application/json' \
               -d '{"sku_id": 1, "customer_id": 7, "qty": 1}'

Read `place_order_naive` below and try to find the bug in it. It opens a
transaction. It checks the stock before it sells. It even maintains a version
column. Every line of it is correct, and it is very close to what SQLAlchemy or
Django emits for `item.stock -= 1`.

The bug is that between the SELECT and the UPDATE the world moved, and the
number it writes back was computed from a fact that stopped being true.
"""
import asyncio
import statistics
import time
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config

state: dict = {}

# Counters the driver cannot see from outside. Reset whenever the mode changes,
# so every scenario is measured on its own.
stats: dict = {}


def reset_stats() -> None:
    stats.clear()
    stats.update(
        sold_out=0,
        check_constraint_violations=0,
        retries_total=0,
        max_retries_one_order=0,
        abandoned_after_max_retries=0,
        errors=0,
        windows_ms=[],
    )


class OrderRequest(BaseModel):
    sku_id: int
    customer_id: int
    qty: int = 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["db"] = await asyncpg.create_pool(config.DATABASE_URL, min_size=5, max_size=40)
    state["http"] = httpx.AsyncClient(
        base_url=config.PRICING_URL, timeout=config.PRICING_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=200),
    )
    reset_stats()
    print(f"[app] mode={config.mode()} pricing={config.PRICING_URL}", flush=True)
    yield
    await state["http"].aclose()
    await state["db"].close()


app = FastAPI(title="System Sense — DB Concurrency Ep.1", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "mode": config.mode()}


async def price_line(sku_id: int, customer_id: int, qty: int) -> int:
    """The work a real handler does between reading a row and writing it back."""
    resp = await state["http"].get(
        "/price", params={"sku_id": sku_id, "customer_id": customer_id, "qty": qty}
    )
    resp.raise_for_status()
    return resp.json()["line_cents"]


SOLD_OUT = JSONResponse({"status": "sold_out"}, status_code=409)


# ── The bug ──────────────────────────────────────────────────────────────────
async def place_order_naive(con, req: OrderRequest):
    """Read the stock, subtract one, write the number back.

    There is nothing wrong with any single line here. There is something wrong
    with the fact that lines 1 and 4 are different statements, and that between
    them another transaction read the same number and is about to write its own
    answer over the top of ours.

    The UPDATE is the part to look at. `SET stock = $2` where $2 was computed in
    Python. Postgres will happily take a literal from you. Under READ COMMITTED
    it even re-reads the row and re-checks the WHERE clause before applying it,
    which is why people believe this is safe: the row it re-reads is fresh, and
    the number it writes is stale.
    """
    row = await con.fetchrow(
        "SELECT stock, version FROM inventory WHERE sku_id = $1", req.sku_id
    )
    if row is None or row["stock"] < req.qty:
        stats["sold_out"] += 1
        return SOLD_OUT

    read_at = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)

    await con.execute(
        "UPDATE inventory SET stock = $2, version = version + 1 WHERE sku_id = $1",
        req.sku_id, row["stock"] - req.qty,
    )
    stats["windows_ms"].append((time.perf_counter() - read_at) * 1000)

    await con.execute(
        "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)",
        req.sku_id, req.customer_id, req.qty,
    )
    return {"status": "confirmed", "mode": "naive"}


# ── Fix one: there is no "between" ───────────────────────────────────────────
async def place_order_atomic(con, req: OrderRequest):
    """One statement. The read and the write are the same operation, so nothing
    can interleave with them, and the `WHERE stock >= $2` is evaluated against
    the row as it is at the instant of writing rather than as it was.

    Zero rows returned is the database telling you it is sold out. Nobody had to
    ask it a question it could not answer."""
    await price_line(req.sku_id, req.customer_id, req.qty)

    row = await con.fetchrow(
        "UPDATE inventory SET stock = stock - $2, version = version + 1"
        " WHERE sku_id = $1 AND stock >= $2 RETURNING stock",
        req.sku_id, req.qty,
    )
    if row is None:
        stats["sold_out"] += 1
        return SOLD_OUT

    await con.execute(
        "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)",
        req.sku_id, req.customer_id, req.qty,
    )
    return {"status": "confirmed", "mode": "atomic", "stock_left": row["stock"]}


# ── Fix two: make everyone wait ──────────────────────────────────────────────
async def place_order_pessimistic(con, req: OrderRequest):
    """Correct, and the pricing call now happens with the row locked.

    Every other customer buying this SKU is blocked for the duration. That is
    not a bug, it is the price, and it is what the throughput column measures."""
    row = await con.fetchrow(
        "SELECT stock, version FROM inventory WHERE sku_id = $1 FOR UPDATE", req.sku_id
    )
    if row is None or row["stock"] < req.qty:
        stats["sold_out"] += 1
        return SOLD_OUT

    read_at = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)

    await con.execute(
        "UPDATE inventory SET stock = $2, version = version + 1 WHERE sku_id = $1",
        req.sku_id, row["stock"] - req.qty,
    )
    stats["windows_ms"].append((time.perf_counter() - read_at) * 1000)

    await con.execute(
        "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)",
        req.sku_id, req.customer_id, req.qty,
    )
    return {"status": "confirmed", "mode": "pessimistic"}


# ── Fix three: ask, and be told you lost ─────────────────────────────────────
async def place_order_optimistic(con, req: OrderRequest):
    """The version column, used properly: the UPDATE only applies if nobody has
    touched the row since we read it, and zero rows means somebody did.

    The pricing call is inside the retry loop on purpose. A price is part of the
    order, and if the transaction has to be redone the price has to be redone
    with it. Hoisting it out would make the window artificially narrow and the
    retry count artificially flattering, which would be measuring a version of
    optimistic locking nobody actually gets to run.
    """
    for attempt in range(1, config.MAX_OPTIMISTIC_RETRIES + 1):
        row = await con.fetchrow(
            "SELECT stock, version FROM inventory WHERE sku_id = $1", req.sku_id
        )
        if row is None or row["stock"] < req.qty:
            stats["sold_out"] += 1
            return SOLD_OUT

        read_at = time.perf_counter()
        await price_line(req.sku_id, req.customer_id, req.qty)

        applied = await con.execute(
            "UPDATE inventory SET stock = $2, version = version + 1"
            " WHERE sku_id = $1 AND version = $3",
            req.sku_id, row["stock"] - req.qty, row["version"],
        )
        stats["windows_ms"].append((time.perf_counter() - read_at) * 1000)

        if applied == "UPDATE 1":
            await con.execute(
                "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)",
                req.sku_id, req.customer_id, req.qty,
            )
            return {"status": "confirmed", "mode": "optimistic", "attempts": attempt}

        # Lost the race. Somebody else's version is in the row now.
        stats["retries_total"] += 1
        stats["max_retries_one_order"] = max(stats["max_retries_one_order"], attempt)

    stats["abandoned_after_max_retries"] += 1
    return JSONResponse(
        {"status": "conflict", "detail": "gave up after too many concurrent updates"},
        status_code=409,
    )


HANDLERS = {
    "naive": place_order_naive,
    "atomic": place_order_atomic,
    "pessimistic": place_order_pessimistic,
    "optimistic": place_order_optimistic,
}


@app.post("/api/orders")
async def place_order(req: OrderRequest):
    handler = HANDLERS[config.mode()]
    try:
        async with state["db"].acquire() as con:
            async with con.transaction():
                return await handler(con, req)
    except asyncpg.CheckViolationError:
        # The guard a reviewer would have asked for. Counted so that "it never
        # fires" is a measurement rather than a claim.
        stats["check_constraint_violations"] += 1
        return JSONResponse({"status": "check_violation"}, status_code=500)
    except Exception as e:  # noqa: BLE001 — anything else is a real failure
        stats["errors"] += 1
        print(f"[app] ERROR {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"status": "error", "detail": type(e).__name__}, status_code=500)


# ── Operating the demo ───────────────────────────────────────────────────────
class ModeRequest(BaseModel):
    mode: str


@app.post("/admin/mode")
async def set_mode(body: ModeRequest):
    """Switch handlers without restarting, so all four scenarios run against one
    stack and one build. The capture script asserts the mode came back."""
    config.set_mode(body.mode)
    reset_stats()
    print(f"[app] mode={config.mode()}", flush=True)
    return {"mode": config.mode()}


@app.get("/admin/stats")
async def read_stats():
    w = sorted(stats["windows_ms"])
    window = {"requests": len(w)}
    if w:
        window |= {
            "min_ms": round(w[0], 1),
            "median_ms": round(statistics.median(w), 1),
            "max_ms": round(w[-1], 1),
        }
    return {k: v for k, v in stats.items() if k != "windows_ms"} | {
        "mode": config.mode(), "window": window
    }


@app.post("/admin/reset")
async def reset(sku_stock: int = 100):
    """Put the shelf back, empty the order book, and forget the counters."""
    async with state["db"].acquire() as con:
        await con.execute("TRUNCATE orders")
        await con.execute(
            "UPDATE inventory SET stock = $1, version = 0 WHERE sku_id = 1", sku_stock
        )
    reset_stats()
    return {"ok": True, "stock": sku_stock}


@app.get("/api/inventory/{sku_id}")
async def read_inventory(sku_id: int):
    """The shelf beside the order book. When these two disagree, the difference
    is stock you have already sold and do not have."""
    async with state["db"].acquire() as con:
        inv = await con.fetchrow(
            "SELECT sku_id, name, stock, version FROM inventory WHERE sku_id = $1", sku_id
        )
        if inv is None:
            return JSONResponse({"detail": "no such sku"}, status_code=404)
        agg = await con.fetchrow(
            "SELECT count(*) AS orders, coalesce(sum(qty), 0) AS units_sold"
            " FROM orders WHERE sku_id = $1",
            sku_id,
        )
    return dict(inv) | {"orders": agg["orders"], "units_sold": int(agg["units_sold"])}
