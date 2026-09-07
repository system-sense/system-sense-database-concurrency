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
import random
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
                 retries_total=0, max_attempts_one_order=0, abandoned=0,
                 # Episode 3: how long each losing transaction was alive before
                 # its engine noticed the cycle.
                 deadlock_wait_ms=[])


def tally(out: Outcome) -> Outcome:
    stats["statuses"][out.status] = stats["statuses"].get(out.status, 0) + 1
    if out.code:
        stats["codes"][out.code] = stats["codes"].get(out.code, 0) + 1
    if out.status in ("deadlocked", "lock_timeout") and out.wait_ms:
        stats["deadlock_wait_ms"].append(out.wait_ms)
    return out


# ── The statements. One set, both engines. ───────────────────────────────────
SQL_SELECT_STOCK = "SELECT stock, version FROM inventory WHERE sku_id = ?"
SQL_UPDATE_LITERAL = "UPDATE inventory SET stock = ?, version = version + 1 WHERE sku_id = ?"
SQL_UPDATE_GUARDED = (
    "UPDATE inventory SET stock = ?, version = version + 1 WHERE sku_id = ? AND stock = ?"
)
SQL_INSERT_ORDER = "INSERT INTO orders (sku_id, customer_id, qty) VALUES (?, ?, ?)"
# Episode 1's fix, unchanged. One statement, so there is no "between": the read
# and the write are the same operation and no update can be lost. Episode 3
# runs exactly this, once per basket line, and it is still correct.
SQL_DECREMENT = "UPDATE inventory SET stock = stock - ? WHERE sku_id = ? AND stock >= ?"
SQL_INSERT_BASKET_ORDER = "INSERT INTO orders (customer_id, qty) VALUES (?, ?)"
SQL_INSERT_ITEM = "INSERT INTO order_items (order_id, sku_id, qty) VALUES (?, ?, ?)"


def sql_update_many(n: int) -> str:
    """One UPDATE covering every basket line.

    Built per call because the two drivers cannot be handed a list through one
    placeholder in the same way, and an IN list with the right arity is the one
    form both dialects accept unchanged.

    Note what is NOT in it: any way to ask for an order. An UPDATE takes no
    ORDER BY. The rows are locked in whatever sequence the plan produces them,
    and the plan is free to change when the statistics move.
    """
    return (
        "UPDATE inventory SET stock = stock - ? WHERE sku_id IN ("
        + ", ".join("?" for _ in range(n))
        + ") AND stock >= ?"
    )
SQL_SELECT_RESERVED = "SELECT id FROM reservations WHERE sku_id = ? FOR SHARE"
SQL_INSERT_RESERVATION = "INSERT INTO reservations (sku_id, customer_id) VALUES (?, ?)"

STATEMENTS = {
    "select_stock": SQL_SELECT_STOCK,
    "update_literal": SQL_UPDATE_LITERAL,
    "update_guarded": SQL_UPDATE_GUARDED,
    "select_reserved": SQL_SELECT_RESERVED,
    "insert_reservation": SQL_INSERT_RESERVATION,
    "decrement": SQL_DECREMENT,
}


class OrderRequest(BaseModel):
    sku_id: int
    customer_id: int
    qty: int = 1
    # Episode 3. The order the customer put things in their basket, exactly as
    # it arrived. Nobody chose it, and that is the whole bug.
    basket: list[int] = []


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


# ── Episode 3: the basket, and the order the rows are taken in ───────────────
async def basket_checkout(cx, req: OrderRequest) -> Outcome:
    """Every line uses Episode 1's atomic decrement. Nothing here is a bug.

    `SQL_DECREMENT` is one statement, so no update can be lost, the stock is
    validated by the database rather than by the application, and the CHECK
    constraint can never fire. This is the code Episode 1 told you to write, and
    it is still right.

    The defect is not in any line. It is in the ORDER of the loop.

    With `LOCK_ORDER=basket` the rows are taken in the order the customer's
    basket happened to arrive in. Customer A's is [7, 12]; customer B's is
    [12, 7]. A takes 7 and waits for 12, B takes 12 and waits for 7, and neither
    will ever let go. There is nothing on screen for a review to catch, because
    the order came from outside the codebase.

    With `LOCK_ORDER=sorted` every transaction agrees on one total order, so the
    cycle cannot close. That is the fix, and it is one word.
    """
    lines = req.basket or [req.sku_id]
    if config.get("lock_order") == "sorted":
        lines = sorted(lines)

    started = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)
    stats["windows_ms"].append((time.perf_counter() - started) * 1000)

    for sku in lines:
        applied = await cx.execute(SQL_DECREMENT, req.qty, sku, req.qty)
        if applied != 1:
            # The shelf really is empty for this line. Not a deadlock, and not
            # the thing this episode is measuring.
            return SOLD_OUT

    order_id = await cx.insert_returning_id(SQL_INSERT_BASKET_ORDER, req.customer_id, len(lines))
    for sku in lines:
        await cx.execute(SQL_INSERT_ITEM, order_id, sku, req.qty)
    return Outcome("confirmed")


async def basket_one_stmt(cx, req: OrderRequest) -> Outcome:
    """The trap fix: "just do it in one statement".

    It is a different claim from "one lock order", and the episode shows it
    failing. A multi-row UPDATE locks the rows in whatever order the plan
    produces them, and the plan is free to change under you when the statistics
    move. Nothing here asks the database for an order, so nothing guarantees it.

    The prelude that DOES hold is the commented line below: take every row you
    are about to touch in one SELECT with an explicit ORDER BY, FOR UPDATE.
    """
    # Deduplicated but NOT sorted. Sorting here would be the actual fix wearing
    # the trap's clothes: the cell is supposed to test whether ONE STATEMENT is
    # enough on its own, so the basket's own order has to survive into it.
    lines = list(dict.fromkeys(req.basket or [req.sku_id]))

    started = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)
    stats["windows_ms"].append((time.perf_counter() - started) * 1000)

    # One statement, many rows. No ORDER BY anywhere, because an UPDATE cannot
    # take one.
    applied = await cx.execute(sql_update_many(len(lines)), req.qty, *lines, req.qty)
    if applied != len(lines):
        return SOLD_OUT

    order_id = await cx.insert_returning_id(SQL_INSERT_BASKET_ORDER, req.customer_id, len(lines))
    for sku in lines:
        await cx.execute(SQL_INSERT_ITEM, order_id, sku, req.qty)
    return Outcome("confirmed")


async def basket_values_join(cx, req: OrderRequest) -> Outcome:
    """The other "one statement" idiom, and the one the storyboard names.

    An IN-list update leaves the engine free to fetch the rows however it likes,
    and on this schema it picks an index order -- the same order for every
    transaction -- so it happens not to deadlock. A VALUES join is different in
    a way that matters: the rows arrive in the order they were written into the
    statement, which is the basket's order, which is the thing nobody chose.

    Neither form PROMISES an order. That is the whole point: one of them is
    currently safe by accident of the plan, and the plan is not a contract.
    """
    lines = list(dict.fromkeys(req.basket or [req.sku_id]))

    started = time.perf_counter()
    await price_line(req.sku_id, req.customer_id, req.qty)
    stats["windows_ms"].append((time.perf_counter() - started) * 1000)

    values = ", ".join(f"({sku}, {req.qty})" for sku in lines)
    applied = await cx.execute(
        f"UPDATE inventory SET stock = inventory.stock - v.qty"
        f" FROM (VALUES {values}) AS v(sku_id, qty)"
        f" WHERE inventory.sku_id = v.sku_id AND inventory.stock >= v.qty"
    )
    if applied != len(lines):
        return SOLD_OUT

    order_id = await cx.insert_returning_id(SQL_INSERT_BASKET_ORDER, req.customer_id, len(lines))
    for sku in lines:
        await cx.execute(SQL_INSERT_ITEM, order_id, sku, req.qty)
    return Outcome("confirmed")


SCENARIOS = {
    "read_modify_write": read_modify_write,
    "count_then_insert": count_then_insert,
    "where_guard": where_guard,
    "basket_checkout": basket_checkout,
    "basket_one_stmt": basket_one_stmt,
    "basket_values_join": basket_values_join,
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
    return {"ok": True, **{k: config.get(k) for k in ("engine", "isolation", "scenario", "lock_order")}}


@app.post("/api/orders")
async def place_order(req: OrderRequest):
    """The retry is OFF by default, and that is the episode's first measurement.

    A deadlock is not a lost update: the victim is rolled back, so nothing
    oversells and the shelf is exactly right. What is wrong is that the order is
    gone. Most applications have no 40P01 handler at all, so the customer is
    simply turned away by the shop's own database while the stock is still
    sitting there. Turning DEADLOCK_RETRIES up is the honest tail, and it is
    measured as its own cell rather than assumed.
    """
    scenario = SCENARIOS[config.get("scenario")]
    isolation = config.get("isolation")
    limit = config.retries()
    deadline = time.perf_counter() + config.RETRY_BUDGET_SECONDS
    for attempt in range(limit + 1):
        out = await engine().run(scenario, req, isolation)
        if out.status not in ("deadlocked", "lock_timeout") or attempt == limit:
            break
        # A budget, not just a count. Without one the retries outlived the load
        # generator's own timeout and every number became a statement about the
        # client rather than about the database.
        if time.perf_counter() >= deadline:
            stats["retry_budget_exhausted"] = stats.get("retry_budget_exhausted", 0) + 1
            break
        stats["retries_total"] += 1
        # Exponential, with jitter and a cap. Jitter matters more than the base
        # here: two transactions that deadlocked are by definition running at
        # the same time, so a fixed backoff walks them straight back into each
        # other. The cap stops a retry storm becoming a convoy.
        backoff = min(0.05 * (2 ** attempt), 0.8)
        await asyncio.sleep(random.uniform(backoff / 2, backoff))
    tally(out)
    code = {"confirmed": 200, "sold_out": 409, "lost_race": 409,
            "deadlocked": 409, "lock_timeout": 409}.get(out.status, 500)
    return JSONResponse(
        {"status": out.status, "code": out.code, "wait_ms": round(out.wait_ms, 1)},
        status_code=code,
    )


# ── Operating the demo ───────────────────────────────────────────────────────
class Cfg(BaseModel):
    engine: str | None = None
    isolation: str | None = None
    scenario: str | None = None
    lock_order: str | None = None
    retries: int | None = None
    lock_timeout_ms: int | None = None


@app.post("/admin/config")
async def set_config(body: Cfg):
    fields = body.model_dump()
    if fields.pop("retries", None) is not None:
        config.set_retries(body.retries or 0)
    if fields.pop("lock_timeout_ms", None) is not None:
        config.set_lock_timeout_ms(body.lock_timeout_ms or 0)
        state["pg"].lock_timeout_ms = config.lock_timeout_ms()
    config.set_all(**fields)
    reset_stats()
    now = {k: config.get(k) for k in ("engine", "isolation", "scenario", "lock_order")}
    now["retries"] = config.retries()
    now["lock_timeout_ms"] = config.lock_timeout_ms()
    print(f"[app] {now}", flush=True)
    return now


@app.get("/admin/stats")
async def read_stats():
    w = sorted(stats["windows_ms"])
    window = {"requests": len(w)}
    if w:
        window |= {"min_ms": round(w[0], 1), "median_ms": round(statistics.median(w), 1),
                   "max_ms": round(w[-1], 1)}
    # NOT "detection latency". This is measured from the transaction opening to
    # the driver raising, so it includes the pricing call and every second spent
    # queued behind other blocked transactions. It is how long a doomed order
    # was alive before the database gave up on it, which is the number the
    # customer actually feels, and it is the honest name for it.
    d = sorted(stats["deadlock_wait_ms"])
    deadlock = {"count": len(d)}
    if d:
        deadlock |= {
            "min_ms": round(d[0], 1),
            "median_ms": round(statistics.median(d), 1),
            "p99_ms": round(d[min(len(d) - 1, int(len(d) * 0.99))], 1),
            "max_ms": round(d[-1], 1),
        }
    return {"statuses": stats["statuses"], "codes": stats["codes"], "window": window,
            "deadlock_wait": deadlock, "retries_configured": config.retries(),
            "lock_timeout_ms": config.lock_timeout_ms(),
            "retries": {"total": stats["retries_total"],
                        "max_one_order": stats["max_attempts_one_order"],
                        "abandoned": stats["abandoned"],
                        "limit": config.MAX_GUARD_RETRIES},
            **{k: config.get(k) for k in ("engine", "isolation", "scenario", "lock_order")}}


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
    # Every SKU, not just the first. Episode 3 spreads the load over a small hot
    # set, so resetting one row would leave seven shelves in whatever state the
    # previous cell left them.
    #
    # Retried, because TRUNCATE wants an exclusive lock and a cell that is still
    # draining will refuse it. That happened: a reset deadlocked against the
    # previous cell's in-flight retries, returned a 500, and took the remaining
    # cells of the matrix with it. Nothing here is measured, so waiting is free.
    for attempt in range(12):
        try:
            await _reset_both(sku_stock)
            break
        except Exception:
            if attempt == 11:
                raise
            await asyncio.sleep(1.0 + attempt)
    reset_stats()
    return {"ok": True, "stock": sku_stock}


async def _reset_both(sku_stock: int) -> None:
    async with state["pg"]._pool.acquire() as con:
        await con.execute("TRUNCATE order_items, orders, reservations")
        await con.execute("UPDATE inventory SET stock = $1, version = 0", sku_stock)
    async with state["my"]._pool.acquire() as con:
        async with con.cursor() as cur:
            await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for t in ("order_items", "orders", "reservations"):
                await cur.execute(f"TRUNCATE TABLE {t}")
            await cur.execute("SET FOREIGN_KEY_CHECKS = 1")
            await cur.execute("UPDATE inventory SET stock = %s, version = 0", (sku_stock,))


@app.get("/api/state")
async def read_state():
    """Both engines' books, side by side. The difference is the episode."""
    out = {}
    async with state["pg"]._pool.acquire() as con:
        out["postgres"] = {
            "stock": await con.fetchval("SELECT stock FROM inventory WHERE sku_id = 1"),
            # Episode 3: the shelf is now eight shelves, and the number that
            # matters is how much is left across all of them while customers
            # are being turned away.
            "stock_total": int(await con.fetchval("SELECT sum(stock) FROM inventory")),
            "orders": await con.fetchval("SELECT count(*) FROM orders"),
            "units_sold": int(await con.fetchval("SELECT coalesce(sum(qty),0) FROM orders")),
            "items_sold": int(await con.fetchval("SELECT coalesce(sum(qty),0) FROM order_items")),
            "reservations": await con.fetchval("SELECT count(*) FROM reservations"),
        }
    async with state["my"]._pool.acquire() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT (SELECT stock FROM inventory WHERE sku_id=1),"
                " (SELECT sum(stock) FROM inventory),"
                " (SELECT count(*) FROM orders), (SELECT coalesce(sum(qty),0) FROM orders),"
                " (SELECT coalesce(sum(qty),0) FROM order_items),"
                " (SELECT count(*) FROM reservations)"
            )
            s1, st, o, u, it, r = await cur.fetchone()
        out["mysql"] = {"stock": s1, "stock_total": int(st), "orders": o,
                        "units_sold": int(u), "items_sold": int(it), "reservations": r}
    return out


@app.get("/api/versions")
async def versions():
    return {"postgres": await state["pg"].version(), "mysql": await state["my"].version()}
