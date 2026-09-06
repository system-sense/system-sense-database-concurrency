# Nobody Chose the Order

**A written companion to Episode 3 of System Sense — [Your Database Is Not Protecting You](../).**

**Watch it instead:** The Deadlock · [full playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)

The video is about thirteen minutes. This covers the same ground more slowly,
with the handler in full, and then goes on into what would not fit: how to read
a Postgres deadlock report line by line, why `deadlock_timeout` is a full second
and what that second costs, when `lock_timeout` is the better answer than a
retry, why "just do it in one statement" is a trap even when it works, and how
to find lock-order bugs in code that has no bug in it.

Every figure here comes from `capture/metrics.json`, produced by
`./scripts/capture-demo.sh` in this folder. Nothing is estimated.

**Who this is for:** you have a transaction that touches more than one row, and
the rows come from user input — a basket, a batch, a bulk edit. By the end you
will know why your correct locking deadlocks anyway, and what the one-word fix
is.

---

## Contents

1. [The failure, in one command](#1-the-failure-in-one-command)
2. [The handler is the previous episode's fix](#2-the-handler-is-the-previous-episodes-fix)
3. [The cycle, drawn](#3-the-cycle-drawn)
4. [The number inverts](#4-the-number-inverts)
5. [Reading a Postgres deadlock report](#5-reading-a-postgres-deadlock-report)
6. [Why one full second](#6-why-one-full-second)
7. [The fix is sorted()](#7-the-fix-is-sorted)
8. [The "one statement" trap](#8-the-one-statement-trap)
9. [lock_timeout, and failing fast](#9-lock_timeout-and-failing-fast)
10. [Retries, and why they are not the fix](#10-retries-and-why-they-are-not-the-fix)
11. [Finding lock-order bugs in code that has no bug](#11-finding-lock-order-bugs-in-code-that-has-no-bug)
12. [What to log](#12-what-to-log)
13. [Exercises](#13-exercises)

---

## 1. The failure, in one command

```bash
docker compose up --build
./scripts/capture-demo.sh
```

Eight SKUs, a hundred units each — eight hundred in total. Three hundred
customers, twenty-five in flight, baskets of two or three **in the order the
customer happened to build them**.

| Cell | Confirmed | Deadlocked | Stock left | Orders/sec |
| --- | --- | --- | --- | --- |
| **basket order · postgres** | **141** | **159** | **446** | **2.9** |
| **basket order · mysql** | **290** | **6** | 55 | **123.2** |
| **sorted order · postgres** | **291** | **0** | 43 | **123.1** |
| **sorted order · mysql** | **291** | **0** | 43 | 123.1 |

Read the first row on its own. **159 customers were turned away while 446 units
were still on the shelves.**

## 2. The handler is the previous episode's fix

```python
async def basket_checkout(cx, req):
    lines = req.basket
    if config.get("lock_order") == "sorted":
        lines = sorted(lines)

    await price_line(req.sku_id, req.customer_id, req.qty)

    for sku in lines:
        applied = await cx.execute(SQL_DECREMENT, req.qty, sku, req.qty)
        if applied != 1:
            return SOLD_OUT
    ...
```

where `SQL_DECREMENT` is Episode 1's atomic fix, unchanged:

```sql
UPDATE inventory SET stock = stock - ? WHERE sku_id = ? AND stock >= ?
```

One statement per line. No lost update is possible. The stock is validated by
the database rather than by the application. The `CHECK` constraint can never
fire. **This is the code the first episode told you to write, and it is still
right.**

The defect is not in any line. It is in the order of the loop — and that order
came from outside the codebase.

## 3. The cycle, drawn

```
Customer A's basket:  [7, 12]          Customer B's basket:  [12, 7]

  A  UPDATE sku 7   -> holds lock on 7
  B  UPDATE sku 12  -> holds lock on 12
  A  UPDATE sku 12  -> waits for B
  B  UPDATE sku 7   -> waits for A
                       ^ neither will ever let go
```

Somebody has to die. The database picks one, rolls it back, and lets the other
through.

**No code review catches this**, because there is nothing on the screen to
catch. Both customers ran identical, correct code. The only difference is the
sequence their items happened to be in, and that came from a click order, a JSON
array, a `SELECT` with no `ORDER BY`, or a dictionary iteration.

## 4. The number inverts

This is the part that makes Episode 3 different from Episodes 1 and 2. **Nothing
oversells.** The victim is rolled back, so the stock is left perfectly correct —
446 units, exactly as it should be.

What is wrong is that **the orders are gone**. 159 customers were told no by a
shop with 446 units on the shelf, and most applications have no `40P01` handler
at all, so nothing anywhere notices.

Same unit as the rest of the series. Opposite sign.

## 5. Reading a Postgres deadlock report

Postgres logs the whole thing. `capture/pglog-pg-basket.log` has it unsummarised;
a single report looks like this:

```
ERROR:  deadlock detected
DETAIL:  Process 1234 waits for ShareLock on transaction 5678; blocked by process 1235.
         Process 1235 waits for ShareLock on transaction 5677; blocked by process 1234.
HINT:  See server log for query details.
CONTEXT:  while updating tuple (0,42) in relation "inventory"
```

Line by line:

- **`DETAIL`** is the cycle itself, one line per participant. Two lines is the
  common case; three or more means a longer chain and is worth alarming on
  separately, because it usually means a table with a hot region rather than a
  pair of unlucky requests.
- **`blocked by process N`** — follow those process ids around the cycle and you
  have the lock order each transaction took.
- **`CONTEXT: while updating tuple (0,42) in relation "inventory"`** is the row
  the victim died on. `(0,42)` is a ctid — block 0, tuple 42. Turn it back into
  a business key with:

  ```sql
  SELECT * FROM inventory WHERE ctid = '(0,42)';
  ```

Turn the logging on yourself — this repo's compose file states both explicitly
rather than inheriting them:

```yaml
command: >
  postgres
  -c deadlock_timeout=1s
  -c log_lock_waits=on
```

`log_lock_waits` prints every wait that outlives `deadlock_timeout`, including
the ones that resolve without a deadlock. Those are the early warning.

On MySQL the equivalent is `--innodb-print-all-deadlocks=ON`, because
`SHOW ENGINE INNODB STATUS` only keeps the *latest* one and "the latest one" is
not evidence.

## 6. Why one full second

**Postgres does not maintain a wait-for graph.** Nothing is watching for cycles.
Instead, a backend that has been blocked for `deadlock_timeout` runs the detector
itself — and the backend that runs the check is the one that gets killed.

The default is one second, and that is not a tuning oversight: building the
graph is expensive, and the overwhelming majority of lock waits resolve on their
own in microseconds. Postgres is betting that waiting a second is cheaper than
checking constantly, and for almost every workload it is right.

Here is what that second cost, measured. This is how long a doomed order stayed
**alive** — from the transaction opening to the driver raising, so it includes
the pricing call and any time queued behind other blocked transactions. It is
not "detection latency" and is deliberately not called that:

| | minimum | median | p99 |
| --- | --- | --- | --- |
| Postgres | **1,092 ms** | **9,054 ms** | **52,969 ms** |
| MySQL | 112 ms | **216 ms** | 293 ms |

That 1,092 ms minimum is `deadlock_timeout` being *measured* rather than quoted.
Nothing can resolve faster than the timeout, because nothing looks sooner.

**InnoDB checks on every lock wait**, so it fails in a fifth of that at the
median and never approaches it at the tail. Read both settings off the engines
rather than trusting this page:

```bash
docker compose exec postgres psql -U sysense -d sysense -c "SHOW deadlock_timeout;"
docker compose exec mysql mysql -usysense -psysense -e "SELECT @@innodb_deadlock_detect;"
```

You can lower `deadlock_timeout`, and the trade is exactly what it looks like:
faster resolution, more CPU spent checking. Do not lower it below your normal
lock hold time or you will run the detector constantly on waits that were about
to succeed.

## 7. The fix is sorted()

```python
for sku in sorted(lines):
```

That is the entire fix.

| | basket order | sorted order |
| --- | --- | --- |
| Deadlocks | **159** | **0** |
| Confirmed | 141 | **291** |
| Orders per second | 2.9 | **123.1** |

A factor of **42** in throughput, for one word. No new lock, no new table, no
new service, no new failure mode.

**Why it works:** a deadlock requires a cycle in the waits-for graph, and a
cycle requires at least two transactions taking the same resources in different
orders. If every transaction in the system takes its rows in one total order,
there is no cycle to form. That is the whole theory, and it is older than any
database you are running.

**What counts as an order** — anything total and stable. The primary key is the
obvious one. It must be the *same* order everywhere, so this is a property of
the system, not of the function: one service sorting by `sku_id` and another
sorting by `name` deadlock exactly as before.

**Where it does not reach:** the sort has to cover every row the transaction
takes, in every code path. A transaction that sorts its basket and then also
touches a `customers` row at the end has two resources and only one of them
ordered.

## 8. The "one statement" trap

The obvious alternative is to do the whole basket in one `UPDATE`. In this
capture, **it worked** — both forms, zero deadlocks:

| | Confirmed | Deadlocked |
| --- | --- | --- |
| one statement, `IN` list | 289 | **0** |
| one statement, `VALUES` join | 291 | **0** |

**And that is exactly the trap.** Look at why it worked — `capture/04-plans.log`:

```
Update on inventory
  ->  Seq Scan on inventory
        Filter: ((stock >= 1) AND (sku_id = ANY ('{8,3,4}'::integer[])))
```

Eight rows, so the planner reads the table straight through. **A sequential scan
hands every transaction the rows in the same physical order**, so they all lock
in the same order and no cycle can form.

Nothing asked for that. Neither statement contains an `ORDER BY`, because an
`UPDATE` cannot take one. The safety is a property of a plan chosen for an
eight-row table, and **a plan is not a contract**. Add an index, grow the table,
let the statistics drift, and the plan changes — while your statement, your
tests and your code review all stay exactly the same.

The idiom that does carry a promise is to take the locks explicitly, in an order
you named, before you touch anything:

```sql
SELECT sku_id FROM inventory WHERE sku_id = ANY($1) ORDER BY sku_id FOR UPDATE;
-- now update in any order you like; every transaction agreed on the sequence
```

`ORDER BY` is legal on a `SELECT`, and `FOR UPDATE` takes the locks as the rows
are produced. That is a guarantee rather than an accident.

## 9. lock_timeout, and failing fast

You do not have to wait a second to find out you lost. `lock_timeout` gives up
on its own:

```sql
SET LOCAL lock_timeout = '50ms';
```

| | basket order | basket + 50 ms `lock_timeout` |
| --- | --- | --- |
| Confirmed | 141 | **274** |
| Failed | 159 (`40P01`) | 26 (`55P03`) |
| p99 order latency | **49,954 ms** | **354 ms** |
| Orders per second | 2.9 | **118.9** |

**A hundred and forty times better at the tail**, and the failures are honest
`55P03` errors that arrive in milliseconds instead of `40P01` after seconds of
stalling.

This is not a fix for the order — the cycles are still forming, you are just
refusing to participate. But it converts a slow outage into a fast error, and a
fast error is something a retry can actually work with. **Ship it alongside
`sorted()`, not instead of it.**

## 10. Retries, and why they are not the fix

Deadlocks cannot be eliminated, only made rare, so a retry is required even once
the order is fixed. But retrying *instead of* fixing the order does not work:

| | Confirmed | Lost | Orders/sec |
| --- | --- | --- | --- |
| basket order, no retry | 141 | 159 | 2.9 |
| basket order, 3 retries | 207 | **93** | 2.7 |
| **sorted order, 3 retries** | **291** | **0** | **123.0** |

Three attempts with exponential backoff recovered some of it and still lost 93
orders, with throughput on the floor. The cycles keep forming; you are just
paying for each one several times.

Two things this demo learned the hard way, both of which are in `app/main.py`:

- **Jitter is not optional.** Two transactions that just deadlocked are, by
  definition, running at the same time. A flat backoff walks them straight back
  into each other. An earlier version of this capture used a fixed 10–50 ms
  jitter and produced a convoy: 418 seconds of wall clock, and 210 of 300
  requests hitting the load generator's own timeout.
- **A budget, not just a count.** `RETRY_BUDGET_SECONDS` caps how long one order
  may spend retrying. Without it the retries outlive the client, and every
  number you collect is a statement about the client's patience rather than the
  database's behaviour.

## 11. Finding lock-order bugs in code that has no bug

There is nothing wrong to grep for, so look for the shape instead:

```bash
# a loop containing an UPDATE or a SELECT ... FOR UPDATE
grep -rnB4 -E "FOR UPDATE|UPDATE .* SET" --include=*.py . | grep -E "for .* in "

# iteration over something that came from a request
grep -rnE "for .* in (req|request|payload|body|items|basket)" .
```

Three questions in review, in order:

1. **Does this transaction touch more than one row?** If not, it cannot be half
   of a cycle. This is why the bug is invisible in unit tests — fixtures have one
   item in them.
2. **Where did the iteration order come from?** If the answer is "the request",
   "a `SELECT` with no `ORDER BY`", "a set", or "a dict", it is unordered and
   the bug is present.
3. **Is the order the same everywhere else that touches these tables?** A total
   order is a property of the system. One service sorting differently reintroduces
   it.

The same reasoning applies beyond rows: **tables have an order too.** If one code
path locks `accounts` then `ledger` and another locks `ledger` then `accounts`,
you have the identical bug at a coarser grain, and it is much harder to see
because the two paths are usually in different files written by different people.

## 12. What to log

- **Deadlocks as their own counter, by SQLSTATE.** `40P01` on Postgres, `1213`
  on MySQL, never merged into a generic "db error". A deadlock rate that is
  non-zero and stable is a lock-order bug you have not found yet.
- **Lock waits that resolve.** `log_lock_waits=on` prints every wait that
  outlived `deadlock_timeout` even when no deadlock followed. These are the near
  misses, and they rise before the deadlocks do.
- **Orders lost to `40P01`, as a business metric.** This is the number that
  inverts: not units oversold, but customers refused while stock remained. If
  your dashboard only counts oversells, this entire episode is invisible on it.
- **The basket size distribution.** Deadlock rate scales with how many rows a
  transaction takes and how concentrated the hot set is. When single-item orders
  dominate, you will not see this in production until the day a promotion changes
  the basket shape.

## 13. Exercises

**1. Hide the bug.** One item per order:

```bash
python3 scripts/order.py --orders 300 --concurrency 25 --max-basket-items 1
```

Deadlocks go to zero, on the same eight SKUs, under the same load — 300
confirmed. A transaction holding one lock can never be half of a cycle. **Not
one line of the bug was fixed**, and every basket in your test fixtures has one
item in it.

**2. Read the cycle.** Run the basket-order cell and then read the log:

```bash
grep -A3 "deadlock detected" capture/pglog-pg-basket.log | head -20
```

Follow the process ids around. Then find the row it died on with the `ctid` from
`CONTEXT`.

**3. Watch the plan change.** Add 5,000 more SKUs, `ANALYZE`, and re-run the
`EXPLAIN` from section 8. When the sequential scan becomes an index scan, ask
yourself what guaranteed the lock order before, and what guarantees it now.

**4. Make the trade yourself.** Run the basket cell with `lock_timeout` at 10 ms,
50 ms and 500 ms. Plot confirmed orders against p99. There is no correct answer,
only a curve you should have seen before production picks a point on it for you.

**5. Find the coarse-grained one.** In your own codebase, list every transaction
that writes to more than one table and write down the table order each of them
uses. If two disagree, you have this bug and no test will ever find it.

---

## Where to go next

`sorted()` worked because **one database could see both locks**. The moment the
critical section leaves the database — a lock in Redis, a lease in etcd, a
worker holding a claim — nothing can see all of them, and the lock you think you
hold may already have expired. That is Episode 4.

- [Episode 1 — the phantom update](../episode-1-lost-update/GUIDE.md): where the
  atomic decrement in section 2 came from.
- [Episode 2 — isolation is a lie](../episode-2-isolation/GUIDE.md): why raising
  the isolation level is not the answer, and what the two engines actually do.

---

Part of the **System Sense — Database Concurrency** mini-series.
