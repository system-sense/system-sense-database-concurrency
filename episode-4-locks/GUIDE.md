# A Lock With a Timeout Is a Lease

**A written companion to Episode 4 of System Sense — [Your Database Is Not Protecting You](../).**

**Watch it instead:** Distributed Locks Without Disaster · [full playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)

The video is about nineteen minutes. This covers the same ground more slowly,
with the handler in full, and then goes on into what would not fit: how to size
a lease against your own critical section, how to reproduce Redlock's
crash-restart on five nodes, the three advisory-lock caveats that actually bite
in production, and how to retrofit fencing onto a table that already has rows.

Every figure here comes from `capture/metrics.json`, produced by
`./scripts/capture-demo.sh` in this folder. Nothing is estimated.

**Who this is for:** you have a critical section that calls somebody else's
system in the middle — a payment, a fulfilment request, a provisioning API — and
you are protecting it with a lock in Redis. By the end you will know why that
lock is a lease, how often yours is expiring already, and what to do about it.

---

## Contents

1. [The failure, in one command](#1-the-failure-in-one-command)
2. [Why a row lock is not available](#2-why-a-row-lock-is-not-available)
3. [A lock with a timeout is a lease](#3-a-lock-with-a-timeout-is-a-lease)
4. [Sizing a lease against your own critical section](#4-sizing-a-lease-against-your-own-critical-section)
5. [The freeze, and why it is not a number](#5-the-freeze-and-why-it-is-not-a-number)
6. [Redlock, and the restart that forgets](#6-redlock-and-the-restart-that-forgets)
7. [Advisory locks, and the three caveats](#7-advisory-locks-and-the-three-caveats)
8. [Fencing tokens, and the reversal](#8-fencing-tokens-and-the-reversal)
9. [Retrofitting a fence onto a live table](#9-retrofitting-a-fence-onto-a-live-table)
10. [What to log](#10-what-to-log)
11. [Exercises](#11-exercises)

---

## 1. The failure, in one command

```bash
docker compose up --build          # in one terminal
./scripts/capture-demo.sh          # in another
```

Six cells, the same 300 customers each time, 80 units across 8 shelves, and a
1000 ms lease. The only thing that moves between cells is which lock is holding
the critical section.

| `LOCK` | parcels | units sold | with no sale | leases expired | writes refused |
| --- | --- | --- | --- | --- | --- |
| `none` | 293 | 80 | 213 | — | — |
| `redis` | 114 | 80 | **34** | 52 | — |
| `redlock` | 113 | 80 | 33 | 52 | — |
| `advisory` | 80 | 80 | **0** | 0 | — |
| `fenced` | 161 | 80 | 81 | 84 | **81** |
| `redis` @ 2600 ms | 80 | 80 | **0** | 0 | — |

A parcel is counted when fulfilment returns, not when the write succeeds,
because that is the moment it stopped being reversible.

## 2. Why a row lock is not available

The critical section is four steps:

```python
row = await con.fetchrow(SQL_EP4_READ, req.sku_id)   # 1. read the stock
if row is None or row["stock"] < req.qty:            # 2. decide
    return SOLD_OUT
parcel = await dispatch(req.sku_id, ...)             # 3. call fulfilment
await con.execute(SQL_EP4_WRITE, row["stock"] - 1)   # 4. write it back
```

Step 3 is what makes this episode different from the three before it. Episodes 1
to 3 put a *pricing* call in the middle of the transaction, and pricing is a
question: ask it twice and nothing has happened in the world. Fulfilment is a
decision somebody else acts on. When it returns a consignment id, a parcel is
moving, and `ROLLBACK` does not reach it.

You *can* hold `SELECT ... FOR UPDATE` across it, and section 7 measures what
that costs. But it pins a pooled connection for the length of somebody else's
HTTP call, and that is a different resource from the one you were protecting.

Note what Episode 1 already proved: when the work stays inside the database you
do not need a lock at all. One atomic statement, and the read and the write are
the same operation. The lock only becomes necessary when the decision leaves.

## 3. A lock with a timeout is a lease

A lock outside the database needs an expiry, because a crashed holder releases
nothing and one dead worker would wedge a SKU forever. That expiry changes what
you are holding:

- a **lock** is released by you, or by your transaction ending
- a **lease** is released by a clock in another process, on a schedule that
  process chose, and **nothing tells you when it happens**

There is no callback. There is no exception. Your worker carries on, doing work
it no longer has the right to do, and the first thing that could possibly notice
is the write — which is where section 8 ends up.

## 4. Sizing a lease against your own critical section

This is the comparison nobody makes, and it takes about ten minutes to make for
your own system.

In this demo the lease is **1000 ms** and the critical section has a median of
**994 ms**, a p95 of **1557 ms**. Those first two numbers are six milliseconds
apart, which is why the failure is not exotic: the lease lands in the middle of
the distribution, so roughly half the work does not fit inside its own lock.

**52 of the 114 parcels** were dispatched by a worker whose lease had already
expired. They overran by a median of **317 ms** and by as much as **566 ms**.

To do this for yourself:

```sql
-- whatever you use for latency. The number you want is the p99 of the WHOLE
-- critical section, lock acquisition to release, not of the HTTP call alone.
SELECT percentile_disc(0.99) WITHIN GROUP (ORDER BY duration_ms)
FROM critical_section_timings WHERE name = 'allocate_and_dispatch';
```

If your lease is not comfortably above that p99, you are already in this
episode. And note the trap: raising the lease past the p99 takes every number in
the table to zero (the last row above) and fixes nothing. The window is
narrower. A stop-the-world GC pause makes it wide again, and not one line of
your application changed.

## 5. The freeze, and why it is not a number

`capture/06-forensic-pause.log` single-steps the mechanism:

```
-- worker-b acquires, then is frozen mid-dispatch
 Container worker-b Paused
-- frozen. waiting out the lease.
-- worker A now takes that lock and sells the same unit:
{"status":"confirmed"}
-- unfreezing worker-b. It has no idea any time passed.
-- worker-b's own result: {"status":"confirmed"}
```

`docker compose pause` uses the cgroup freezer, which is a fair stand-in for a
stop-the-world GC pause or hypervisor steal: the process cannot run, cannot
heartbeat, cannot renew, and has no idea time passed when it wakes.

**This produces no figure the episode quotes.** It is the mechanism slowed down
until it is visible. Every number in the table above comes from the unattended
fleet, where which workers overrun is decided by arithmetic on the ids.

## 6. Redlock, and the restart that forgets

Redlock scores the same as a single node here — 113 parcels against 114, 33 with
no sale against 34 — and it should, because it is an *availability* algorithm.
It answers "what if a lock server goes down". It has no opinion about how long
your work takes.

Kleppmann's objection is that its safety argument rests on timing assumptions:
bounded pauses and bounded clock drift. The pause half is measured above. The
clock-skew half is **argued, not measured**, here and in the video: faking a
container's clock needs `libfaketime` or `CAP_SYS_TIME` and reads as a trick.

What *can* be measured is the crash-restart, and the setup is the whole trick.
**Three nodes is not enough** — a client holding all three survives a restart,
because the next client can only reach the one that forgot. You need the holder
on a *bare* majority:

```bash
docker compose stop redis-d redis-e      # a 5-node quorum tolerates this
# worker-b acquires a, b, c  -> three of five, a valid bare majority
docker compose pause worker-b            # so it keeps holding
docker compose start redis-d redis-e     # they were never told about the lock
docker compose restart redis-c           # no persistence: it forgets
```

Now `c`, `d` and `e` have no record of the lock, and the next client acquires
all three. From `capture/07-redlock-restart.log`:

```
   redis-a  holds: w911
   redis-b  holds: w911
   redis-c  holds: w912
   redis-d  holds: w912
   redis-e  holds: w912
```

Two clients, three nodes each, out of five, at the same instant. Both then sold
the same single unit.

**antirez's rebuttal is fair and you should know it:** a node that restarts
should delay before serving again, for at least one lock lifetime, and then it
cannot contribute to a second quorum while the first is still valid. That works,
and it is in the specification. It is also another timing assumption underneath
the first one, and almost nobody configures it or tests it.

## 7. Advisory locks, and the three caveats

`pg_advisory_xact_lock` is the first honest answer in the episode: **80 parcels,
80 units sold, nothing without a sale.** There is no TTL, so there is nothing to
expire; the lock is owned by a session and dies with it.

```python
async with pg.acquire() as con:
    tx = con.transaction()
    await tx.start()
    await con.execute("SELECT pg_advisory_xact_lock($1)", req.sku_id)
    ...                       # the whole critical section, including dispatch
    await tx.commit()         # and the lock is released here, by definition
```

The cost is real and smaller than the folklore suggests: waiting for a pool
connection reached **156 ms at p99 over 300 acquisitions**. Whether that stays
survivable is your pool size against your call latency, and you can measure both
today.

The caveats are where this goes wrong in production:

1. **A connection pooler in transaction mode silently breaks session-scoped
   advisory locks**, because your next statement can land on another backend.
   Use the `_xact_` variant, which is scoped to the transaction. `pg_advisory_lock`
   behind PgBouncer in transaction mode is a lock that quietly is not one.
2. **A half-open connection holds its lock until TCP keepalive gives up.** The
   client is gone; the kernel has not noticed. Tune `tcp_keepalives_idle` and
   friends, which almost nobody does.
3. **They live in the normal lock manager.** They appear in `pg_locks` and they
   participate in deadlock detection — so two workers taking two advisory locks
   in two different orders will deadlock with `40P01`, exactly as in Episode 3.
   Sort them.

```sql
-- your advisory locks, and who is waiting on them
SELECT pid, granted, objid, classid FROM pg_locks WHERE locktype = 'advisory';
```

If every worker that needs this critical section talks to one Postgres, **this
is the one to ship.** The moment a second service, region or queue consumer
needs it, you are back to a lock with a lease in it.

## 8. Fencing tokens, and the reversal

Stop trying to build a lock that cannot be lost. Assume it will be lost, and
make the *write* refuse.

Every acquisition mints a strictly higher token. The row remembers which token is
current. The write asks whether it is still the holder:

```sql
-- on the way IN: claim the row. A newer token supersedes an older claim.
UPDATE inventory SET fence_token = $1 WHERE sku_id = $2 AND fence_token < $1;

-- on the WRITE: does this row still think I am the holder?
UPDATE inventory SET stock = $1 WHERE sku_id = $2 AND fence_token = $3;
```

The claim on the way in is not optional, and this is the part that is easy to get
wrong. Stamping the token only at write time protects nothing when the stale
worker happens to finish **first**: its token is still the highest the row has
seen, so it is accepted and the newer holder's write lands on top. Measured here
rather than reasoned — the first implementation of this refused **1 stale write
out of 30 expired leases.** Claiming on entry refused **81 of 84**.

A real refusal from the run: a worker came back carrying token **39**, the row
said **40**, and the write returned `UPDATE 0`.

**Now the part that is not in the textbooks.** Fencing gets the shelf exactly
right — 80 of 80 units — and it makes the parcel count **worse**: 81 parcels with
nothing sold behind them, against 34 with the plain Redis lock.

The reason is mechanical. A refused write does not decrement the stock, so the
unit is still on the shelf, so the next worker finds it and dispatches. The thing
that protected the data is the thing that kept the shelf looking available.

**Fencing did not reduce the damage. It moved it** — out of your database and
into your loading bay. Your inventory is now correct and your vans are busier,
and if you only watch the database, which is the part that is now correct, you
will not see any of it.

Because fencing stopped the *write*. It never had any way to stop the fulfilment
call the frozen worker already made. The parcel is on a van, which is where the
Idempotency series begins.

## 9. Retrofitting a fence onto a live table

You cannot add `fence_token` and start enforcing it in one deploy; existing
writers carry no token and every write would be refused.

```sql
-- 1. add it, defaulted, non-breaking. Existing writers keep working.
ALTER TABLE inventory ADD COLUMN fence_token BIGINT NOT NULL DEFAULT 0;

-- 2. deploy writers that CLAIM and carry a token but do not yet enforce.
--    Log the mismatches instead of refusing them: that log is how you find out
--    how often your lease is already expiring, before you break anything.

-- 3. once the mismatch rate is understood, switch the guard on.
```

Step 2 is the valuable one even if you never ship step 3. It turns "does our
lock expire mid-work?" from an argument into a count.

## 10. What to log

Almost nobody logs any of this, which is why it is invisible:

- **whether the lock was still yours at release.** A hygienic compare-and-delete
  release returns 0 when the lock was no longer yours. That return value is free
  and it is the cheapest possible smoke detector. This demo counts it as
  `released_not_ours`.
- **critical section duration, as a distribution**, against your configured
  lease. One number next to the other is the whole of section 4.
- **`UPDATE 0` from a fenced write**, with both tokens. A refused write is not an
  error to swallow; it is the only evidence you will ever get that a lock failed.
- **the gap between side effects and committed writes.** Parcels dispatched
  against units sold. If those two counters ever diverge, you are in this
  episode, and no single-system metric shows it.

## 11. Exercises

1. **Find your own lease-to-p99 ratio.** Section 4. It is ten minutes of work
   and it either reassures you or it does not.
2. **Turn `LOCK_TTL_MS` up to 2600 and watch every number go to zero.** Then
   explain to somebody why nothing was fixed.
3. **Break the fence on purpose.** Change `fence_token = $3` to
   `fence_token <= $3` in `app/main.py` and re-run the `fenced` cell. Work out
   why the refusals collapse.
4. **Reproduce the Redlock restart with three nodes instead of five** and
   satisfy yourself that it cannot be done, then say precisely why.
5. **Make `advisory` deadlock.** Give each customer a two-item basket and take
   the advisory locks in basket order. Episode 3, by a different road.

---

## Where to go next

Fencing refused the write. It could not refuse the parcel that had already
shipped, and a parcel on a van for an order the system does not believe it
accepted is exactly where the **Idempotency** series starts.

- [Episode 1 — the phantom update](../episode-1-lost-update/GUIDE.md): why an
  atomic statement needs no lock at all.
- [Episode 2 — isolation is a lie](../episode-2-isolation/GUIDE.md): what the
  two engines do with the same setting.
- [Episode 3 — nobody chose the order](../episode-3-deadlock/GUIDE.md): the
  deadlock that advisory locks hand you back.

---

Part of the **System Sense — Database Concurrency** mini-series.
