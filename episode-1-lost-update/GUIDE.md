# The Gap Between the Read and the Write

**A written companion to Episode 1 of System Sense — [Your Database Is Not Protecting You](../).**

**Watch it instead:** [The Phantom Update](https://youtu.be/IU96-QIg7no) · [full playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)

The video is about eleven minutes. This covers the same ground more slowly, with
the code in full, and then goes on into what would not fit: why the `CHECK`
constraint a reviewer asks for cannot help you, what an ORM emits for
`item.stock -= 1` and why that is the whole bug, how to choose between the four
fixes on something other than taste, and how to find this in a codebase you did
not write.

Every figure here comes from `capture/metrics.json`, produced by
`./scripts/capture-demo.sh` in this folder. Nothing is estimated.

**Who this is for:** you have a counter — stock, seats, credits, a balance — that
more than one request can change, and you have wrapped the change in a
transaction. By the end you will know why that transaction did not help, and
which of the four fixes belongs in your situation.

---

## Contents

1. [The failure, in one command](#1-the-failure-in-one-command)
2. [The handler, and why it passes review](#2-the-handler-and-why-it-passes-review)
3. [What actually happened, statement by statement](#3-what-actually-happened-statement-by-statement)
4. [The constraint that never fires](#4-the-constraint-that-never-fires)
5. [Why the transaction did not save you](#5-why-the-transaction-did-not-save-you)
6. [Fix 1 — one statement](#6-fix-1--one-statement)
7. [Fix 2 — SELECT … FOR UPDATE](#7-fix-2--select--for-update)
8. [Fix 3 — a version column](#8-fix-3--a-version-column)
9. [Choosing between them](#9-choosing-between-them)
10. [How to find this in a codebase you did not write](#10-how-to-find-this-in-a-codebase-you-did-not-write)
11. [What to log](#11-what-to-log)
12. [When you can ignore all of this](#12-when-you-can-ignore-all-of-this)
13. [Exercises](#13-exercises)

---

## 1. The failure, in one command

```bash
docker compose up --build
./scripts/capture-demo.sh
```

One hundred units of one SKU. Three hundred customers, twenty-five in flight at
a time, one unit each.

| | `naive` |
| --- | --- |
| Orders confirmed | **300** |
| Units that existed | 100 |
| Units oversold | **200** |
| Stock the database still offers | **88** |
| Errors returned to anybody | **0** |
| `CHECK (stock >= 0)` violations | **0** |

Read rows one, two and four together, because that is the whole episode. Three
hundred people bought one of a hundred seats. Every one of them was confirmed.
Nobody saw an error. And the shelf still says eighty eight.

Those three numbers cannot all be true, and the system believes all three.

## 2. The handler, and why it passes review

```python
async def place_order_naive(con, req):
    row = await con.fetchrow(
        "SELECT stock, version FROM inventory WHERE sku_id = $1", req.sku_id
    )
    if row is None or row["stock"] < req.qty:
        return SOLD_OUT

    await price_line(req.sku_id, req.customer_id, req.qty)

    await con.execute(
        "UPDATE inventory SET stock = $2, version = version + 1 WHERE sku_id = $1",
        req.sku_id, row["stock"] - req.qty,
    )
    await con.execute(
        "INSERT INTO orders (sku_id, customer_id, qty) VALUES ($1, $2, $3)",
        req.sku_id, req.customer_id, req.qty,
    )
```

It runs inside a transaction. It checks the stock before it sells. It even
maintains a `version` column. There is no defect in any individual line.

**And it is, line for line, what an ORM emits for `item.stock -= 1`.** That is
not an accident of this demo. `item.stock -= 1` is a read into application
memory, an arithmetic operation in Python, and a write of the resulting literal.
SQLAlchemy, Django and ActiveRecord all produce exactly this shape unless you go
out of your way to ask for something else, which is section 6.

## 3. What actually happened, statement by statement

Two requests, arriving milliseconds apart:

```
T1  SELECT stock  ->  100
T2  SELECT stock  ->  100          both read the same number
T1  price_line(...)                 185 ms of real work
T2  price_line(...)
T1  UPDATE stock = 99               T1 writes 100 - 1
T2  UPDATE stock = 99               T2 writes 100 - 1, over the top of T1
T1  INSERT order                    two orders
T2  INSERT order
```

Two orders exist. One decrement survives. The other was not rejected, not
logged, and not lost in transit — it was **overwritten by a write that was
itself perfectly valid**.

Measured across the naive run: the gap between the `SELECT` and the `UPDATE` was
**185.2 ms** at the median, 62.9 ms at the fastest and 356.1 ms at the slowest.
Every request that starts inside another request's gap reads a number that is
already stale.

The arithmetic of the run: 300 units sold, **288 decrements written over**, 12
survived. `100 - 12 = 88`, which is the number on the shelf.

## 4. The constraint that never fires

`db/init.sql` puts the guard a reviewer asks for on the table:

```sql
CONSTRAINT stock_never_negative CHECK (stock >= 0)
```

It fired **0** times in 300 oversold orders.

This is worth sitting with, because it is the most common false comfort in this
whole area. **A lost update does not write a negative number. It writes a
plausible one.** Every individual `UPDATE` in that run set stock to 99, and 99
satisfies `stock >= 0` perfectly. The constraint is not weak; it is answering a
question nobody asked.

Nor is there anything to `GROUP BY` looking for anomalies. The order count is
right. The customer ids are distinct. One column is wrong and nothing in the row
says so.

## 5. Why the transaction did not save you

Because a transaction gives you **atomicity** and **durability**, and this bug is
neither.

- *Atomicity* means all of your statements or none. Both transactions ran all of
  their statements.
- *Durability* means a committed write survives a crash. Both writes survived.
- *Isolation* is the one you wanted, and at `READ COMMITTED` — the default on
  Postgres, Oracle and SQL Server — it does not promise that a row you read stays
  unchanged until you write it. It promises you will not read *uncommitted*
  data. T2 read committed data. It was simply old by the time it was used.

Raising the isolation level is the obvious next move, and it is
[Episode 2](../episode-2-isolation/) — where it turns out that `REPEATABLE READ`
means two different things on the two databases you are most likely to run.

## 6. Fix 1 — one statement

```sql
UPDATE inventory SET stock = stock - $2 WHERE sku_id = $1 AND stock >= $2
```

There is no "between" here, because the read and the write are the same
operation. `stock - $2` is evaluated by the database against the row as it is at
the moment of writing, under a row lock it takes and releases itself. Zero rows
affected is the database telling you it is sold out.

| | `naive` | `atomic` |
| --- | --- | --- |
| Orders confirmed | 300 | **100** |
| Units oversold | **200** | **0** |
| Stock left | 88 | 0 |
| Customers turned away | 0 | 200 sold out |
| Orders per second | 114.1 | **122.8** |

**It is also the fastest of the four.** That is the part people do not expect:
the correct version outperformed the broken one, because it holds no lock across
the pricing call and makes one round trip instead of two.

**How to get it out of an ORM.** This is the shape you want and the reason it is
worth knowing your ORM's escape hatch:

```python
# SQLAlchemy — an UPDATE, not a read-modify-write
session.query(Inventory).filter(
    Inventory.sku_id == sku, Inventory.stock >= qty
).update({Inventory.stock: Inventory.stock - qty})

# Django — F() defers the arithmetic to the database
Inventory.objects.filter(sku_id=sku, stock__gte=qty).update(
    stock=F("stock") - qty
)
```

Both emit `SET stock = stock - N`. Both check `.rowcount` — and if you do not
check it, you have swapped a silent oversell for a silent under-sell.

## 7. Fix 2 — SELECT … FOR UPDATE

```sql
SELECT stock FROM inventory WHERE sku_id = $1 FOR UPDATE
```

Correct, and expensive in a specific way. The row lock is held from the `SELECT`
to the `COMMIT`, and in this handler the pricing call is inside that window. So
every other order for that SKU queues behind it.

| | `atomic` | `pessimistic` |
| --- | --- | --- |
| Time to make one sale, p50 | 183 ms | **4,688 ms** |
| Orders per second | 122.8 | **15.6** |

Four point seven seconds a sale, and correct. **The lock is not the problem; the
work inside it is.** If you need `FOR UPDATE` — and sometimes you genuinely do,
when the decision involves several rows — then get everything slow out of the
transaction first. Price the order, call the fraud service, resolve the tax
rate, and *then* open the transaction that takes the lock.

This mode is also where Episode 3 begins: `FOR UPDATE` over more than one row,
taken in the order the customer's basket happened to be in.

## 8. Fix 3 — a version column

```sql
UPDATE inventory SET stock = $2, version = version + 1
 WHERE sku_id = $1 AND version = $3
```

Optimistic locking. You read version 7, and you will only write if it is still
version 7. Zero rows means somebody moved it, so read again and redo the work.

| | `optimistic` |
| --- | --- |
| Orders confirmed | 86 |
| Units oversold | 0 |
| Retries | **1,161** |
| Customers refused after 5 attempts | **214** |
| Units left unsold on a shelf that had them | **14** |

Correct, and **not free**. Under this much contention on one row, optimistic
locking spends most of its time losing races. It is the right tool when
conflicts are rare and the transaction is long — a document being edited, a
config record, a user profile — and the wrong tool for a single hot counter.

Note the last row. Fourteen units did not sell, on a shelf that had them,
because their would-be buyers ran out of attempts. **Correctness has a cost and
it is denominated in customers**, which is a theme the series returns to in
Episode 3.

The pricing call is deliberately inside the retry loop in this demo. A price is
part of the order; if the write is redone the price has to be redone with it.
Hoisting it out would make the window artificially narrow and the retry count
artificially flattering.

## 9. Choosing between them

| Situation | Reach for |
| --- | --- |
| A counter, and the new value is a function of the old | **one statement** |
| The decision spans several rows, or needs application logic | `FOR UPDATE`, with the slow work moved out first |
| Long-lived edits, conflicts genuinely rare | a **version column** |
| A hot single row under heavy contention | one statement — optimistic locking will thrash |

The question that decides it is not "which is safest" — all three are safe. It
is **how long the critical section has to be, and how often two people are
actually in it at once.**

## 10. How to find this in a codebase you did not write

Grep is unusually effective here, because the bug has a shape.

```bash
# Python: arithmetic on an attribute that came from the database
grep -rnE '\.(stock|balance|count|quantity|credits)\s*[-+]=' .

# The ORM idiom, before it becomes an UPDATE
grep -rnE '\.save\(\)|session\.commit\(\)' . | head

# SQL where the right-hand side is a bound parameter rather than the column
grep -rnE 'SET\s+\w+\s*=\s*[\$%:?]' .
```

The tell in review is a **`SELECT` and an `UPDATE` of the same row in the same
function, with anything at all between them.** The distance between them is the
size of the bug. If what is between them is a network call, the bug is roughly
the latency of that call wide.

The second tell is an `UPDATE` whose row count is not checked. Every fix in
sections 6–8 returns "zero rows" as its way of saying no, and a handler that
ignores that has all of the cost and none of the protection.

## 11. What to log

The reason this bug survives to production is that nothing in the normal
telemetry moves. Requests succeed, latency is flat, error rate is zero. Two
things are worth emitting:

- **Rows affected, on every mutating statement**, as a counter you can alert on.
  `UPDATE 0` on what should be a certain write is the single highest-signal
  event in this whole area.
- **A periodic invariant check** that does the arithmetic the application will
  not: `SELECT sum(qty) FROM orders` against `stock_before - stock_now`. Alert
  when they diverge. It is cheap, it is boring, and it is the only thing in this
  section that would have caught the naive run.

Do not bother alerting on the `CHECK` constraint. See section 4.

## 12. When you can ignore all of this

Genuinely, sometimes:

- **The value is idempotent, not incremental.** `SET status = 'shipped'` does not
  care who wrote last. Last-writer-wins is the correct semantics.
- **One writer by construction.** A single-threaded consumer of a partitioned
  log, where the partition key is the row key, has no concurrent writer to lose
  a write to.
- **The counter is advisory.** View counts, "N people are looking at this",
  approximate analytics. Losing 96% of your increments is fine if nobody makes a
  decision on the number.

The line is whether **something downstream refuses to be wrong**: money,
inventory, seats, capacity, quota. If a human gets an email when the number is
wrong, it is not advisory.

## 13. Exercises

**1. Hide the bug.** Narrow the race window and watch the oversell collapse
without changing a line of the handler:

```bash
PRICING_BASE_MS=1 PRICING_SPREAD_MS=2 docker compose up --build
```

Fewer requests land inside a shorter window. This is why it passes on a laptop,
passes in staging, and sells 200 seats that do not exist on the day of the drop.

**2. Hide it the other way.** Run the app with a single worker and a pool size of
one. The oversell disappears because the concurrency did, and your staging
environment probably looks exactly like this.

**3. Watch the decrements being lost.** Run the naive mode and then ask the
database to do the arithmetic:

```bash
docker compose exec postgres psql -U sysense -d sysense -c \
  "SELECT (SELECT count(*) FROM orders) AS orders,
          100 - (SELECT stock FROM inventory WHERE sku_id=1) AS decrements_that_survived;"
```

**4. Break the atomic fix on purpose.** Remove `AND stock >= $2` from the
one-statement version and run it. Now the `CHECK` constraint finally fires — and
your customers get a 500 instead of "sold out". Correct and unusable is a real
category.

**5. Find one in your own codebase.** Use the greps in section 10. Then for each
hit, ask the only question that matters: *is there a network call between the
read and the write?*

---

## Where to go next

The obvious next move is to raise the isolation level, because that is what it is
for. **It means two different things on the two databases you are most likely to
be running**, and finding out which is
[Episode 2](../episode-2-isolation/) — with the guide at
[`episode-2-isolation/GUIDE.md`](../episode-2-isolation/GUIDE.md).

- [Episode 3 — the deadlock](../episode-3-deadlock/GUIDE.md): correct locks,
  taken in an order nobody chose, and a full shelf with the customers turned away.
- Episode 4 — distributed locks: what happens when the critical section leaves
  the database and nothing can see every lock.

---

Part of the **System Sense — Database Concurrency** mini-series.
