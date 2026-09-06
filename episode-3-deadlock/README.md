# Episode 3 — The Deadlock

**System Sense — Database Concurrency**, episode 3 of 4.
*A full shelf, and the customer was turned away by your own database.*

The handler in this folder is **Episode 1's fix**. One atomic statement per
line, stock validated by the database, all inside one transaction. There is no
defect in any line of it. It still loses a third of the orders.

```bash
docker compose up --build          # in one terminal
./scripts/capture-demo.sh          # in another — runs all ten cells
```

> **[GUIDE.md](GUIDE.md)** is the written companion: the same ground more
> slowly, plus what would not fit — how to read a Postgres deadlock report line
> by line, why `deadlock_timeout` is a full second, when `lock_timeout` beats a
> retry, and how to find lock-order bugs in code that has no bug in it.


## The one table to remember

Eight SKUs of 100 units each, **800 in total**. Three hundred customers,
twenty-five in flight, baskets of two or three **in the order the customer
built them**.

| Cell | Confirmed | Deadlocked | Stock left | Orders/sec |
| --- | --- | --- | --- | --- |
| **basket order · postgres** | **141** | **159** | **446** | **2.9** |
| **basket order · mysql** | **290** | **6** | 55 | **123.2** |
| **sorted order · postgres** | **291** | **0** | 43 | **123.1** |
| **sorted order · mysql** | **291** | **0** | 43 | 123.1 |
| basket + 3 retries · postgres | 207 | 93 | 278 | 2.7 |
| basket + 50ms lock_timeout · postgres | 274 | 0 | 99 | 118.9 |
| one item per order · postgres | 300 | 0 | 500 | 122.5 |

Read the first row on its own. **159 customers were turned away while 446 units
were still on the shelves.** Nothing oversold — a deadlock victim is rolled
back, so the stock is perfectly correct. The orders are simply gone, and most
applications have no `40P01` handler to notice.

Every figure comes from `capture/metrics.json`. Nothing is estimated.

## The bug is not in the code

Customer A's basket is `[7, 12]`. Customer B's is `[12, 7]`.

A takes 7 and waits for 12. B takes 12 and waits for 7. Neither will ever let
go, so the database kills one of them. **The iteration order came from the
customer**, and no code review catches it because there is nothing on the screen
to catch.

## The fix is one word

```python
for sku in sorted(lines):      # <- that is the entire fix
```

Deadlocks go from **159 to 0** and throughput from **2.9 to 123.1 orders a
second**. No new lock, no new table, no new service.

## The two engines do not notice at the same speed

How long a doomed order stayed alive — transaction open to driver raise, so it
includes the pricing call and any time queued behind other blocked transactions.
It is not "detection latency" and is not called that:

| | minimum | median | p99 |
| --- | --- | --- | --- |
| Postgres | **1,092 ms** | **9,054 ms** | **52,969 ms** |
| MySQL | 112 ms | **216 ms** | 293 ms |

That Postgres minimum is `deadlock_timeout` being **measured**: Postgres keeps
no wait-for graph, so nothing even looks for a cycle until a backend has waited
a full second, and the backend that runs the check is the one that dies. InnoDB
checks on every lock wait.

```bash
docker compose exec postgres psql -U sysense -d sysense -c "SHOW deadlock_timeout;"
```

**Failing fast beats waiting to be chosen.** A 50 ms `lock_timeout` gives 274
confirmed instead of 141, and a p99 of **354 ms instead of 49,954 ms**.

## The "one statement" idea, and why it is a trap

Both one-statement forms measured **zero deadlocks** here. That is not a
recommendation, it is the trap:

```
Update on inventory
  ->  Seq Scan on inventory
        Filter: ((stock >= 1) AND (sku_id = ANY ('{8,3,4}'::integer[])))
```

Eight rows, so the planner reads the table straight through, and a sequential
scan hands every transaction the same physical order. **It worked because of a
plan nobody asked for.** Neither statement contains an `ORDER BY` — an `UPDATE`
cannot take one — so nothing promises this survives an index, a bigger table, or
a change in the statistics. `capture/04-plans.log` has both plans.

The prelude that actually holds a promise is
`SELECT … WHERE id = ANY($1) ORDER BY id FOR UPDATE`.

## The knob

```bash
LOCK_ORDER=sorted docker compose up --build
```

**Try this — hide the bug without fixing it.** `MAX_BASKET_ITEMS=1`:

```bash
python3 scripts/order.py --orders 300 --concurrency 25 --max-basket-items 1
```

Deadlocks go to zero, on the same eight SKUs, under the same load. A
transaction holding one lock can never be half of a cycle. **Not one line of the
bug was fixed** — and every basket in your staging fixtures has one item in it.

## What is where

```
app/main.py         the checkout, and the one line that sorts
app/config.py       LOCK_ORDER, retries, lock_timeout, MAX_BASKET_ITEMS
db/init.sql         inventory, orders, and order_items — the basket
scripts/order.py    the customers, and the baskets they built
scripts/capture-demo.sh   the ten cells, the plans, and the lock logs
capture/            the measured output every number above comes from
capture/04-plans.log      why one statement happened to be safe
```

Counts move by a few between runs. The split does not: hundreds of deadlocks on
Postgres in basket order, single digits on MySQL, zero on both when sorted.

---

Previous: **[Episode 2 — Transaction Isolation Is a Lie](../episode-2-isolation/)**,
where one level name meant two different guarantees.

Next: **Episode 4 — Distributed Locks Without Disaster**. `sorted()` worked
because one database could see both locks. The moment the critical section
leaves the database, nothing can.

Part of the **System Sense — Database Concurrency** mini-series ·
[playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)
