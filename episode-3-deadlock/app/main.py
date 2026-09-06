"""System Sense — DB Concurrency Ep.2: the same code, two databases.

Run it:   docker compose up --build

Episode 1 ended on a setting. This episode turns it on, and the only honest way
to show what it does is to run the same statements against two engines at the
same named level and let them disagree.

The three scenarios below are written ONCE. They never branch on the engine, and
the only thing that differs between what Postgres and MySQL are sent is the
placeholder syntax -- `$1` against `%s` -- which app/engines.py handles and
`/admin/sql` prints side by side so it can be checked rather than believed.
"""
import asyncio
import statistics
import time
from contextlib import asynccontextmanager

import aiomysql
import asyncpg
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config
from .engines import MySQL, Outcome, Postgres, sql_for

state: dict = {}
stats: dict = {}


def reset_stats() -> None:
    stats.clear()
    stats.update(codes={}, statuses={}, windows_ms=[],
                 retries_total=0, max_attempts_one_order=0, abandoned=0)


def tally(out: Outcome) -> Outcome:
    stats["statuses"][out.status] = stats["statuses"].get(out.status, 0) + 1
    if out.code:
        stats["codes"][out.code] = stats["codes"].get(out.code, 0) + 1
    return out


# ── The statements. One set, both engines. ───────────────────────────────────
SQL_SELECT_STOCK = "SELECT stock, version FROM inventory WHERE sku_id = ?"
SQL_UPDATE_LITERAL = "UPDATE inventory SET stock = ?, version = version + 1 WHERE sku_id = ?"
SQL_UPDATE_GUARDED = (
    "UPDATE inventory SET stock = ?, version = version + 1 WHERE sku_id = ? AND stock = ?"
)
SQL_INSERT_ORDER = "INSERT INTO orders (sku_id, customer_id, qty) VALUES (?, ?, ?)"
SQL_SELECT_RESERVED = "SELECT id FROM reservations WHERE sku_id = ? FOR SHARE"
SQL_INSERT_RESERVATION = "INSERT INTO reservations (sku_id, customer_id) VALUES (?, ?)"

STATEMENTS = {
    "select_stock": SQL_SELECT_STOCK,
    "update_literal": SQL_UPDATE_LITERAL,
    "update_guarded": SQL_UPDATE_GUARDED,
    "select_reserved": SQL_SELECT_RESERVED,
    "insert_reservation": SQL_INSERT_RESERVATION,
}


class OrderRequest(BaseModel):
    sku_id: int
    customer_id: int
    qty: int = 1


SOLD_OUT = Outcome("sold_out")


async def price_line(sku_id: int, customer_id: int, qty: int) -> int:
    resp = await state["http"].get(
        "/price", params={"sku_id": sku_id, "customer_id": customer_id, "qty": qty}
    )
    resp.raise_for_status()
    return resp.json()["line_cents"]


# ── Scenario B: read, change it in Python, write it back ─────────────────────
async def read_modify_write(cx, req: OrderRequest) -> Outcome:
    row = await cx.fetchrow(SQL_SELECT_STOCK, req.sku_id)
    if row is None or row["stock"] < req.qty:
        return SOLD_OUT

    started = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)

    await cx.execute(SQL_UPDATE_LITERAL, row["stock"] - req.qty, req.sku_id)
    stats["windows_ms"].append((time.perf_counter() - started) * 1000)
    await cx.execute(SQL_INSERT_ORDER, req.sku_id, req.customer_id, req.qty)
    return Outcome("confirmed")


# ── Scenario C: count, decide there is room, insert ──────────────────────────
async def count_then_insert(cx, req: OrderRequest) -> Outcome:
    """Nothing is updated here, so nothing conflicts.

    Two transactions read the same set, both decide there is room, and both
    insert a DIFFERENT row. There is no lost update anywhere: every write is
    kept. The invariant is what breaks, and no row-level mechanism can see it.

    The `FOR SHARE` is deliberate and it is the beat of the episode. It is the
    strongest lock you can take on rows you are only reading, and on Postgres it
    still locks nothing but the rows that already exist.
    """
    stock_row = await cx.fetchrow(SQL_SELECT_STOCK, req.sku_id)
    if stock_row is None:
        return SOLD_OUT
    held = await cx.fetchall(SQL_SELECT_RESERVED, req.sku_id)
    if len(held) + req.qty > stock_row["stock"]:
        return SOLD_OUT

    started = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)
    stats["windows_ms"].append((time.perf_counter() - started) * 1000)

    await cx.execute(SQL_INSERT_RESERVATION, req.sku_id, req.customer_id)
    return Outcome("confirmed")


# ── The portable fix: put the value you read into the WHERE clause ───────────
async def where_guard(cx, req: OrderRequest) -> Outcome:
    """No isolation level involved, and identical on both engines.

    `WHERE stock = <the value I read>` is a current read on Postgres and on
    MySQL alike: it is evaluated against the row as it is at the moment of
    writing. Zero rows means somebody moved it under you.

    Losing the race is not the same as being sold out, so the loop is not
    optional. This is Episode 1's optimistic mode, and Episode 1 gave it five
    attempts; measured without one, the guard books about twenty of the hundred
    seats and the episode's landing looks worse than the levels it is arguing
    against. What is being compared is the fix, not the contention.

    The pricing call stays inside the loop for the reason Episode 1 gave: a
    price is part of the order, so if the write has to be redone the price has
    to be redone with it. Hoisting it out would narrow the window artificially
    and flatter the retry count.

    The retries happen inside the one transaction, which is what makes this
    portable: at READ COMMITTED both engines give every statement a fresh
    snapshot, so the re-read sees the value that beat us. Nothing here branches
    on the engine.
    """
    for attempt in range(1, config.MAX_GUARD_RETRIES + 1):
        row = await cx.fetchrow(SQL_SELECT_STOCK, req.sku_id)
        if row is None or row["stock"] < req.qty:
            stats["max_attempts_one_order"] = max(stats["max_attempts_one_order"], attempt)
            return SOLD_OUT

        started = time.perf_counter()
        await price_line(req.sku_id, req.customer_id, req.qty)
        stats["windows_ms"].append((time.perf_counter() - started) * 1000)

        applied = await cx.execute(
            SQL_UPDATE_GUARDED, row["stock"] - req.qty, req.sku_id, row["stock"]
        )
        stats["max_attempts_one_order"] = max(stats["max_attempts_one_order"], attempt)
        if applied == 1:
            await cx.execute(SQL_INSERT_ORDER, req.sku_id, req.customer_id, req.qty)
            return Outcome("confirmed")

        # Zero rows. Somebody else's number is in the column now, so read it
        # again and price the order against what is actually on the shelf.
        stats["retries_total"] += 1

    stats["abandoned"] += 1
    return Outcome("lost_race")


SCENARIOS = {
    "read_modify_write": read_modify_write,
    "count_then_insert": count_then_insert,
    "where_guard": where_guard,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pg"] = Postgres(await asyncpg.create_pool(config.DATABASE_URL, min_size=5, max_size=40))
    state["my"] = MySQL(
        await aiomysql.create_pool(
            host=config.MYSQL_HOST, port=config.MYSQL_PORT, user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD, db=config.MYSQL_DB, minsize=5, maxsize=40,
            autocommit=True, pool_recycle=300,
        )
    )
    state["http"] = httpx.AsyncClient(
        base_url=config.PRICING_URL, timeout=config.PRICING_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=200),
    )
    reset_stats()
    print("[app] both engines up", flush=True)
    yield
    await state["http"].aclose()


app = FastAPI(title="System Sense — DB Concurrency Ep.2", lifespan=lifespan)


def engine():
    return state["pg"] if config.get("engine") == "postgres" else state["my"]


@app.get("/health")
async def health():
    return {"ok": True, **{k: config.get(k) for k in ("engine", "isolation", "scenario")}}


@app.post("/api/orders")
async def place_order(req: OrderRequest):
    scenario = SCENARIOS[config.get("scenario")]
    out = tally(await engine().run(scenario, req, config.get("isolation")))
    code = {"confirmed": 200, "sold_out": 409, "lost_race": 409}.get(out.status, 500)
    return JSONResponse({"status": out.status, "code": out.code}, status_code=code)


# ── Operating the demo ───────────────────────────────────────────────────────
class Cfg(BaseModel):
    engine: str | None = None
    isolation: str | None = None
    scenario: str | None = None


@app.post("/admin/config")
async def set_config(body: Cfg):
    config.set_all(**body.model_dump())
    reset_stats()
    now = {k: config.get(k) for k in ("engine", "isolation", "scenario")}
    print(f"[app] {now}", flush=True)
    return now


@app.get("/admin/stats")
async def read_stats():
    w = sorted(stats["windows_ms"])
    window = {"requests": len(w)}
    if w:
        window |= {"min_ms": round(w[0], 1), "median_ms": round(statistics.median(w), 1),
                   "max_ms": round(w[-1], 1)}
    return {"statuses": stats["statuses"], "codes": stats["codes"], "window": window,
            "retries": {"total": stats["retries_total"],
                        "max_one_order": stats["max_attempts_one_order"],
                        "abandoned": stats["abandoned"],
                        "limit": config.MAX_GUARD_RETRIES},
            **{k: config.get(k) for k in ("engine", "isolation", "scenario")}}


@app.get("/admin/sql")
async def show_sql():
    """The same statements as each engine receives them.

    This endpoint exists so the episode's central claim can be verified rather
    than asserted: the only difference is the placeholder.
    """
    return {
        name: {"postgres": sql_for("postgres", sql), "mysql": sql_for("mysql", sql)}
        for name, sql in STATEMENTS.items()
    }


@app.post("/admin/reset")
async def reset(sku_stock: int = 100):
    """Both shelves back, both books empty. Always both, so a scenario can never
    be measured against a state the other engine left behind."""
    async with state["pg"]._pool.acquire() as con:
        await con.execute("TRUNCATE orders, reservations")
        await con.execute("UPDATE inventory SET stock = $1, version = 0 WHERE sku_id = 1", sku_stock)
    async with state["my"]._pool.acquire() as con:
        async with con.cursor() as cur:
            await cur.execute("TRUNCATE TABLE orders")
            await cur.execute("TRUNCATE TABLE reservations")
            await cur.execute("UPDATE inventory SET stock = %s, version = 0 WHERE sku_id = 1", (sku_stock,))
    reset_stats()
    return {"ok": True, "stock": sku_stock}


@app.get("/api/state")
async def read_state():
    """Both engines' books, side by side. The difference is the episode."""
    out = {}
    async with state["pg"]._pool.acquire() as con:
        out["postgres"] = {
            "stock": await con.fetchval("SELECT stock FROM inventory WHERE sku_id = 1"),
            "orders": await con.fetchval("SELECT count(*) FROM orders"),
            "units_sold": int(await con.fetchval("SELECT coalesce(sum(qty),0) FROM orders")),
            "reservations": await con.fetchval("SELECT count(*) FROM reservations"),
        }
    async with state["my"]._pool.acquire() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT (SELECT stock FROM inventory WHERE sku_id=1),"
                " (SELECT count(*) FROM orders), (SELECT coalesce(sum(qty),0) FROM orders),"
                " (SELECT count(*) FROM reservations)"
            )
            s, o, u, r = await cur.fetchone()
        out["mysql"] = {"stock": s, "orders": o, "units_sold": int(u), "reservations": r}
    return out


@app.get("/api/versions")
async def versions():
    return {"postgres": await state["pg"].version(), "mysql": await state["my"].version()}
