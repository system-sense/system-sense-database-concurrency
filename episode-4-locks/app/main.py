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
from .locks import LockUnavailable, build as build_lock

state: dict = {}
stats: dict = {}


def reset_stats() -> None:
    stats.clear()
    stats.update(codes={}, statuses={}, windows_ms=[],
                 retries_total=0, max_attempts_one_order=0, abandoned=0,
                 # Episode 3: how long each losing transaction was alive before
                 # its engine noticed the cycle.
                 deadlock_wait_ms=[],
                 # Episode 4. `dispatched` counts parcels, and it is counted at
                 # the moment fulfilment returns rather than after the write,
                 # because that is when the thing became irreversible.
                 dispatched=0, lease_expired=0, fenced_out=0,
                 lock_unavailable=0, released_not_ours=0,
                 lease_overrun_ms=[], fenced_tokens=[], pool_wait_ms=[])


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


async def allocate_under_advisory(pg, req: OrderRequest) -> Outcome:
    """pg_advisory_xact_lock, and the whole critical section inside ONE
    transaction on ONE connection.

    This is not the same shape as the other four and it is not meant to be.
    There is no TTL, so there is nothing to expire and no lease to believe in:
    the lock is held by this transaction and it is released when this
    transaction ends, whether that is a commit, a rollback, or the backend
    dying. A worker paused for forty seconds still holds it when it wakes up,
    and nothing can be handed out underneath it. That is a genuinely different
    safety story from the first three modes, and it is the first honest answer
    in the episode.

    It is also not free, and the cost is visible right here rather than argued:
    the transaction is open across `dispatch`, so this pins a pool connection
    for the entire external call. That is the trade. Measure it before
    dismissing it -- and note that whenever the work stays inside the database,
    this is simply the right answer.
    """
    waited = time.perf_counter()
    async with pg.acquire() as con:
        stats["pool_wait_ms"].append((time.perf_counter() - waited) * 1000)
        tx = con.transaction()
        await tx.start()
        try:
            # Blocks until it is ours. It does not time out, which is the
            # point; and because it lives in the normal lock manager it shows
            # up in pg_locks and participates in deadlock detection -- Episode
            # 3 arriving again by a different road.
            await con.execute("SELECT pg_advisory_xact_lock($1)", req.sku_id)
            row = await con.fetchrow(SQL_EP4_READ, req.sku_id)
            if row is None or row["stock"] < req.qty:
                await tx.commit()
                return SOLD_OUT

            started = time.perf_counter()
            await dispatch(req.sku_id, req.customer_id, req.qty)
            stats["dispatched"] = stats.get("dispatched", 0) + 1
            stats["windows_ms"].append((time.perf_counter() - started) * 1000)

            await con.execute(SQL_EP4_WRITE, row["stock"] - req.qty, req.sku_id)
            await con.execute(SQL_EP4_INSERT_ORDER, req.sku_id, req.customer_id, req.qty)
            await tx.commit()
            return Outcome("confirmed")
        except BaseException:
            await tx.rollback()
            raise


# ── Episode 4: the decision that spans somebody else's system ────────────────
# These are the only statements in the app written with Postgres's own
# placeholders. Everything else in the series is written once with `?` and
# rewritten per driver by app/engines.py -- but Episode 4 runs against a raw
# connection rather than through PgCx, because its critical section is not a
# transaction, so there is nothing in the path to do the rewriting.
SQL_EP4_READ = "SELECT stock, fence_token FROM inventory WHERE sku_id = $1"
SQL_EP4_WRITE = "UPDATE inventory SET stock = $1 WHERE sku_id = $2"
# The punchline of the series. `<` and not `<=`, because a token stays current
# across many writes and only a LOWER one is stale. Zero rows updated is the
# storage layer refusing a writer whose lock is gone -- the only component in
# the whole series in a position to refuse it.
# Stamped at ACQUISITION, not at write time, and the difference is the whole
# mechanism. A token written only at the end does not protect anything when the
# stale worker happens to finish FIRST: its token is still the highest the row
# has seen, so it is accepted, and the newer holder's write lands on top. Both
# writes succeed and the shelf is wrong. Measured, not reasoned: the first cut
# of this refused 1 stale write out of 30 expired leases.
#
# Claiming the row on the way IN is what makes the guard meaningful. The
# acquisition stamp uses `<` because a newer token may always supersede an
# older claim.
SQL_EP4_CLAIM = (
    "UPDATE inventory SET fence_token = $1 WHERE sku_id = $2 AND fence_token < $1"
)
# ...and the write itself uses `=`, which reads as the only question worth
# asking at that point: does this row still think I am the holder? A worker
# whose lease expired and whose claim was taken over by somebody else answers
# no, and gets UPDATE 0.
SQL_EP4_WRITE_FENCED = (
    "UPDATE inventory SET stock = $1 WHERE sku_id = $2 AND fence_token = $3"
)
SQL_EP4_INSERT_ORDER = (
    "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)"
)


async def dispatch(sku_id: int, worker_id: int, qty: int) -> dict:
    """Hand a parcel to fulfilment. There is no undo.

    This is the line that makes the episode: after it returns, something has
    happened in the world. ROLLBACK does not reach it, the lock does not reach
    it, and the fencing token does not reach it either -- fencing refuses the
    WRITE, and by then the van has already left.
    """
    resp = await state["fulfil"].post(
        "/dispatch", params={"sku_id": sku_id, "worker_id": worker_id, "qty": qty}
    )
    resp.raise_for_status()
    return resp.json()


async def allocate_and_dispatch(pg, req: OrderRequest, lock) -> Outcome:
    """Read the stock, decide, dispatch a parcel, write the stock back.

    Note what this is NOT doing wrong. Read-modify-write is exactly what a
    distributed lock is FOR: inside a correctly held lock it is safe, and
    Episode 1's atomic decrement is not available here because the decision
    depends on work that happens outside the database. The handler is holding a
    lock across its critical section, which is the textbook thing to do.

    The bug is that the lock is a lease, and the lease can run out in the middle
    of the dispatch. Nothing tells this function that. It carries on.
    """
    mode = config.get("lock")
    if mode == "advisory":
        # Different shape, and the difference IS the safety story. See below.
        return await allocate_under_advisory(pg, req)

    key = f"sku:{req.sku_id}"
    owner = f"w{req.customer_id}"
    fenced = mode == "fenced"

    try:
        held = await lock.acquire(key, owner)
    except LockUnavailable:
        stats["lock_unavailable"] = stats.get("lock_unavailable", 0) + 1
        return Outcome("lock_unavailable")

    try:
        async with pg.acquire() as con:
            if fenced:
                # Claim the row for this token before reading it. From here on
                # the row itself knows who the current holder is, and it is the
                # only participant that cannot be fooled by an expired lease.
                await con.execute(SQL_EP4_CLAIM, held.token, req.sku_id)
            row = await con.fetchrow(SQL_EP4_READ, req.sku_id)
        if row is None or row["stock"] < req.qty:
            return SOLD_OUT

        started = time.perf_counter()
        parcel = await dispatch(req.sku_id, req.customer_id, req.qty)
        # Counted the moment it happens, not after the write succeeds. A parcel
        # dispatched by a worker whose write is later refused is still a parcel.
        stats["dispatched"] = stats.get("dispatched", 0) + 1
        stats["windows_ms"].append((time.perf_counter() - started) * 1000)

        # The observation the whole episode turns on. The application does not
        # do this -- no real one does -- but the capture has to, so that "the
        # lease expired mid-work" is a measured count rather than an assertion.
        held.lease_expired = held.ttl_ms > 0 and not await lock.still_held(held)
        if held.lease_expired:
            stats["lease_expired"] = stats.get("lease_expired", 0) + 1
            stats["lease_overrun_ms"].append(held.held_for_ms() - held.ttl_ms)

        async with pg.acquire() as con:
            if fenced:
                applied = await con.execute(
                    SQL_EP4_WRITE_FENCED, row["stock"] - req.qty, req.sku_id, held.token
                )
                if applied.split()[-1] == "0":
                    # UPDATE 0. This worker's token is behind the row's, so its
                    # lock was handed to somebody else while it was working.
                    stats["fenced_out"] = stats.get("fenced_out", 0) + 1
                    stats["fenced_tokens"].append(
                        {"worker": owner, "carried": held.token,
                         "row_had": row["fence_token"], "rows": 0}
                    )
                    return Outcome("fenced_out", "", "stale token refused")
            else:
                await con.execute(SQL_EP4_WRITE, row["stock"] - req.qty, req.sku_id)
            await con.execute(SQL_EP4_INSERT_ORDER, req.sku_id, req.customer_id, req.qty)
        return Outcome("confirmed")
    finally:
        await lock.release(held)
        if not held.released_cleanly and lock.name != "none":
            # We could not release it because it was no longer ours. The
            # hygienic Lua release is what makes this visible at all; a bare DEL
            # would have silently deleted somebody else's lock instead.
            stats["released_not_ours"] = stats.get("released_not_ours", 0) + 1


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
    # Episode 4's takes the pool and the lock rather than a connection already
    # inside a transaction, because its critical section is not a transaction.
    "allocate_and_dispatch": allocate_and_dispatch,
}

#  Scenarios that manage their own transactions. Everything before Episode 4
#  ran inside one; this one spans an external call and must not.
UNWRAPPED = {"allocate_and_dispatch"}


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
    # Episode 4. A separate client, because it is a separate service with a
    # different timeout profile: pricing answers in tens of milliseconds and
    # fulfilment takes the better part of a second and a half.
    state["fulfil"] = httpx.AsyncClient(
        base_url=config.FULFILMENT_URL, timeout=config.FULFILMENT_TIMEOUT_SECONDS,
        limits=httpx.Limits(max_connections=200),
    )
    state["lock"] = build_lock(config.get("lock"), state["pg"])
    reset_stats()
    print("[app] both engines up", flush=True)
    yield
    await state["http"].aclose()
    await state["fulfil"].aclose()


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
    name = config.get("scenario")
    scenario = SCENARIOS[name]
    if name in UNWRAPPED:
        out = tally(await scenario(state["pg"], req, state["lock"]))
        code = {"confirmed": 200}.get(out.status, 409)
        return JSONResponse(
            {"status": out.status, "code": out.code, "detail": out.detail},
            status_code=code,
        )
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
    lock: str | None = None
    lock_ttl_ms: int | None = None


@app.post("/admin/config")
async def set_config(body: Cfg):
    fields = body.model_dump()
    if fields.pop("retries", None) is not None:
        config.set_retries(body.retries or 0)
    if fields.pop("lock_timeout_ms", None) is not None:
        config.set_lock_timeout_ms(body.lock_timeout_ms or 0)
        state["pg"].lock_timeout_ms = config.lock_timeout_ms()
    if fields.pop("lock_ttl_ms", None) is not None:
        config.set_lock_ttl_ms(body.lock_ttl_ms or 1)
    config.set_all(**fields)
    # Rebuilt whenever the mode changes: `redis` and `fenced` share a lock
    # implementation on purpose, and `advisory` needs the pool.
    state["lock"] = build_lock(config.get("lock"), state["pg"])
    reset_stats()
    now = {k: config.get(k) for k in ("engine", "isolation", "scenario", "lock_order", "lock")}
    now["retries"] = config.retries()
    now["lock_timeout_ms"] = config.lock_timeout_ms()
    now["lock_ttl_ms"] = config.lock_ttl_ms()
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
    def pct(xs: list[float]) -> dict:
        if not xs:
            return {"count": 0}
        v = sorted(xs)
        return {"count": len(v), "median_ms": round(statistics.median(v), 1),
                "p95_ms": round(v[min(len(v) - 1, int(len(v) * 0.95))], 1),
                "p99_ms": round(v[min(len(v) - 1, int(len(v) * 0.99))], 1),
                "max_ms": round(v[-1], 1)}

    # ── Episode 4 ────────────────────────────────────────────────────────────
    #  `dispatched` is parcels, not orders, and the gap between it and what the
    #  shelf says is the oversell. `lease_expired` is the mechanism behind that
    #  gap, counted directly rather than inferred, and `fenced_out` is the
    #  storage layer refusing a writer whose lock had already gone.
    lock4 = {
        "mode": config.get("lock"),
        "ttl_ms": config.lock_ttl_ms(),
        "dispatched": stats.get("dispatched", 0),
        "lease_expired": stats.get("lease_expired", 0),
        "fenced_out": stats.get("fenced_out", 0),
        "lock_unavailable": stats.get("lock_unavailable", 0),
        "released_not_ours": stats.get("released_not_ours", 0),
        "critical_section": pct(stats["windows_ms"]),
        "lease_overrun": pct(stats["lease_overrun_ms"]),
        "pool_wait": pct(stats["pool_wait_ms"]),
        # Kept whole so the episode can put real token values on the board
        # beside the UPDATE 0 that refused them.
        "fenced_examples": stats["fenced_tokens"][:5],
    }
    return {"statuses": stats["statuses"], "codes": stats["codes"], "window": window,
            "deadlock_wait": deadlock, "retries_configured": config.retries(),
            "lock_timeout_ms": config.lock_timeout_ms(),
            "lock": lock4,
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
