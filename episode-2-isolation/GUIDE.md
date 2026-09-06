# One Level Name, Two Different Guarantees

**A written companion to Episode 2 of System Sense — [Your Database Is Not Protecting You](../).**

**Watch it instead:** Transaction Isolation Is a Lie · [full playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)

The video is about fifteen minutes. This covers the same ground more slowly,
with the statements in full, and then goes on into what would not fit: what the
SQL standard actually defines, why Postgres and MySQL both satisfy it while
disagreeing completely, what `FOR SHARE` does and does not lock, how to read a
lock view while a load is running, and how to write the retry loop that every
one of these levels assumes you already have.

Every figure here comes from `capture/metrics.json`, produced by
`./scripts/capture-demo.sh` in this folder. Nothing is estimated.

**Who this is for:** you read Episode 1, or you already knew about lost updates,
and you reached for `REPEATABLE READ`. By the end you will know what that setting
does on each engine, why the same code oversells on one and aborts on the other,
and why the fix this episode lands on is not a setting at all.

---

## Contents

1. [The matrix, in one command](#1-the-matrix-in-one-command)
2. [What the standard actually says](#2-what-the-standard-actually-says)
3. [The same statements, both engines](#3-the-same-statements-both-engines)
4. [Row B — the lost update at REPEATABLE READ](#4-row-b--the-lost-update-at-repeatable-read)
5. [Row C — count, then insert](#5-row-c--count-then-insert)
6. [Reading the lock views yourself](#6-reading-the-lock-views-yourself)
7. [SERIALIZABLE, and what it costs](#7-serializable-and-what-it-costs)
8. [The portable fix](#8-the-portable-fix)
9. [What the fix costs on each engine](#9-what-the-fix-costs-on-each-engine)
10. [The retry loop every level assumes you have](#10-the-retry-loop-every-level-assumes-you-have)
11. [A cheat sheet for the level you are on](#11-a-cheat-sheet-for-the-level-you-are-on)
12. [What to log](#12-what-to-log)
13. [Exercises](#13-exercises)

---

## 1. The matrix, in one command

```bash
docker compose up --build
./scripts/capture-demo.sh
```

One hundred units of one SKU. Three hundred customers, twenty-five in flight,
one seat each. **PostgreSQL 16.15 and MySQL 8.4.11, same load, same run.**

| Cell | Booked | Oversold | Aborted | Driver code |
| --- | --- | --- | --- | --- |
| `READ COMMITTED` · read-modify-write · pg | 300 | **200** | 0 | — |
| `READ COMMITTED` · read-modify-write · my | 300 | **200** | 0 | — |
| **`REPEATABLE READ` · read-modify-write · pg** | 20 | **0** | **280** | `40001` |
| **`REPEATABLE READ` · read-modify-write · my** | 300 | **200** | **0** | none raised |
| **`REPEATABLE READ` · count-then-insert · pg** | 124 | **24** | **0** | none raised |
| **`REPEATABLE READ` · count-then-insert · my** | 5 | **0** | **295** | `1213` |
| `SERIALIZABLE` · read-modify-write · pg | 21 | 0 | 279 | `40001` |
| `SERIALIZABLE` · read-modify-write · my | 12 | 0 | 288 | `1213` |
| `READ COMMITTED` · WHERE-guard · pg | 86 | **0** | 0 | — |
| `READ COMMITTED` · WHERE-guard · my | 100 | **0** | 0 | — |

Read the four bold rows as two pairs. **At the same level name, with the same
statements, the two engines swap which one is safe — in both directions.**

## 2. What the standard actually says

The SQL standard defines four isolation levels, and it defines them by **what
they forbid**, not by how they work. Three phenomena:

| Phenomenon | What it is |
| --- | --- |
| dirty read | you see another transaction's uncommitted write |
| non-repeatable read | you read a row twice in one transaction and it changed |
| phantom | you run a query twice and a new row has appeared in the answer |

| Level | Forbids |
| --- | --- |
| `READ UNCOMMITTED` | nothing |
| `READ COMMITTED` | dirty reads |
| `REPEATABLE READ` | dirty reads, non-repeatable reads |
| `SERIALIZABLE` | all three |

**Notice what is not on that list.** There is no clause about two transactions
both *writing*. The standard describes symptoms to avoid and leaves the
machinery entirely to the vendor — and the machinery is what you actually get.

- **Postgres** implements it with snapshots. Your transaction sees the database
  as of the moment it began, and if you try to write over a row somebody has
  changed since, it cannot let you commit. It aborts you with SQLSTATE `40001`
  and tells you to retry.
- **MySQL/InnoDB** uses snapshots for reads, but a write is not a read. An
  `UPDATE` performs a *current read*: it takes a lock, waits its turn, reads the
  row as it is now, and applies your statement to it.

Both honestly satisfy the definition. Neither is cheating. They are about to
produce opposite outcomes from identical code.

## 3. The same statements, both engines

The episode's central claim is that the application never branches on the
engine. There is one set of statements, and the only difference is the
placeholder:

```
postgres   UPDATE inventory SET stock = $1, version = version + 1 WHERE sku_id = $2
mysql      UPDATE inventory SET stock = %s, version = version + 1 WHERE sku_id = %s
```

Check it rather than believe it:

```bash
curl -s localhost:8000/admin/sql | python3 -m json.tool
```

That endpoint exists for exactly this reason. `app/engines.py` translates `?`
into `$1, $2, …` for asyncpg and `%s` for aiomysql, and that translation is the
entire difference between what the two engines are sent.

## 4. Row B — the lost update at REPEATABLE READ

Episode 1's handler, unchanged, at `REPEATABLE READ`.

**Postgres:** 20 orders confirmed, **280 transactions aborted**, every one with

```
SQLSTATE 40001 — could not serialize access due to concurrent update
```

Zero oversold. It is loud, it is ugly, and it is correct.

**MySQL:** 300 orders confirmed. Zero aborted. **Zero errors of any kind.** Two
hundred units oversold.

MySQL did exactly what it was told. It took the lock, waited, read the row as it
is *now* — and then wrote the number your application had computed from the row
as it *was*. The snapshot governs your reads; it does not govern the value you
hand back.

**This is the single most consequential difference in the episode**, because the
MySQL side is silent. There is no error to catch, no log line, no metric that
moves.

## 5. Row C — count, then insert

Now change the shape. Instead of updating a counter, count rows and insert one —
which is what reservations look like in every booking system:

```sql
SELECT id FROM reservations WHERE sku_id = ? FOR SHARE   -- how many are held?
INSERT INTO reservations (sku_id, customer_id) VALUES (?, ?)
```

**Nothing here is a lost update.** Every write survives. Nobody overwrites
anybody. Two transactions both count, both see room, and both insert a
*different* row. There is no conflict, because nothing was updated — and the
invariant breaks anyway.

**Postgres:** 124 reservations on 100 seats. **24 oversold. Zero aborts, zero
errors, nothing in the log.**

`FOR SHARE` is the strongest lock you can take on rows you are only reading, and
it locks **the rows that exist**. It cannot lock a row that has not been inserted
yet, and Postgres has no gap locks at `REPEATABLE READ`, so there was nothing in
the way.

**MySQL:** 5 confirmed, **295 deadlocks**, errno `1213`, zero oversold.

InnoDB's next-key locking locks the gaps *between* index records as well as the
records. An insert that would land inside your result set has to wait, and when
enough of them wait on each other, the engine starts killing transactions.

**The engines have swapped sides.** Neither vendor's `REPEATABLE READ` is the
other's.

The index shape is load-bearing here and the capture verifies it from the engine
rather than assuming it:

```
reservations  1  reservations_sku_idx  1  sku_id  A  ...     <- Non_unique = 1
```

A unique index matching an existing row takes a record lock only; a non-unique
one takes a next-key lock, gap included.

## 6. Reading the lock views yourself

**They only exist while a load is running.** Once the last transaction commits
both views are empty, which is the whole difficulty of showing anyone a lock.
So run a load in one terminal:

```bash
python3 scripts/order.py --orders 300 --concurrency 25
```

and look in another:

```bash
# MySQL — the gap locks, named by the engine
docker compose exec mysql mysql -usysense -psysense sysense -e \
  "SELECT OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE, LOCK_STATUS, count(*)
     FROM performance_schema.data_locks GROUP BY 1,2,3,4,5;"

# Postgres — and count what is WAITING, which is the interesting column
docker compose exec postgres psql -U sysense -d sysense -c \
  "SELECT locktype, mode, granted, count(*) FROM pg_locks GROUP BY 1,2,3 ORDER BY 4 DESC;"
```

On count-then-insert, MySQL shows

```
reservations  reservations_sku_idx  RECORD  X,INSERT_INTENTION  WAITING  15
```

`INSERT_INTENTION … WAITING` on the secondary index **is** the gap lock. Postgres,
running identical code at the same moment, shows **zero** ungranted locks.

Reading `data_locks` needs two grants, and `PROCESS` alone is not enough — it
fails with a confusing "SELECT command denied". `db/init.mysql.sql` grants both
so a clone can run the query above:

```sql
GRANT PROCESS ON *.* TO 'sysense'@'%';
GRANT SELECT ON performance_schema.* TO 'sysense'@'%';
```

## 7. SERIALIZABLE, and what it costs

Both engines are safe at `SERIALIZABLE`. Zero oversold, on either bug. Here is
the bill, out of 300 customers:

| | Got through | Failed | As |
| --- | --- | --- | --- |
| Postgres | 21 | **279** | `40001` serialization failure |
| MySQL | 12 | **288** | `1213` deadlock |

Those aborts are not the database failing. **That is the database working** — it
is telling you, correctly, that it could not produce a serial ordering and that
you should try again.

Which is fine, except your application does not have a retry loop. It has an
exception handler that returns a 500. So you have converted a silent correctness
bug into a very loud availability bug, and you will find out on the day the
traffic arrives.

Note also that Postgres reaches safety by *aborting* and MySQL by *deadlocking*.
The currency differs even when the answer is the same.

## 8. The portable fix

Put the value you read into the `WHERE` clause.

```sql
UPDATE inventory SET stock = $1, version = version + 1
 WHERE sku_id = $2 AND stock = $3        -- $3 is the value I read
```

If somebody moved it while you were pricing the order, that statement matches
nothing and the database reports zero rows changed. **Zero rows is not an error.
It is an answer:** you were working from a number that is no longer true, so read
it again.

It is a *current read* on both engines. It needs no isolation level, no setting
and no vendor documentation, and it behaves the same way in both databases.

You have also seen it before — it is Episode 1's optimistic locking, with the
stock column doing the version column's job. That is not a coincidence and it is
worth checking: Episode 1 measured 86 booked, 1,161 retries and 214 customers
abandoned; this episode's Postgres guard measured **86, 1,186 and 214**.

## 9. What the fix costs on each engine

Both oversell nothing. **The fix is portable in correctness** — and the two
engines charge for it in completely different currencies.

| | Postgres | MySQL |
| --- | --- | --- |
| Booked, of 100 | 86 | **100** |
| Oversold | 0 | 0 |
| Customers refused after 5 attempts | **214** | **0** |
| Retries | **1,186** | 133 |
| Time to make one sale, p50 | 219 ms | **4,785 ms** |

Before concluding MySQL won that round, look at where those 4.8 seconds went:

```
inventory  PRIMARY  RECORD  X,REC_NOT_GAP  WAITING  23
inventory  PRIMARY  RECORD  X,REC_NOT_GAP  GRANTED   1
```

One holder, twenty-three queued behind it. That is not a faster database, it is
a **queue** — MySQL is serialising every customer behind one row and handing them
the answer in turn, which is why almost nobody has to retry and why one sale
takes 4.8 seconds. Postgres's matching sample has **zero** ungranted locks:
nobody waits, so the losers spin and after five attempts 214 of them are refused.

**4,785 ms should look familiar.** Episode 1 measured `SELECT … FOR UPDATE` at
4,688 ms and rejected it for being too slow. There is no lock in this code. We
wrote a `WHERE` clause and MySQL handed us a queue anyway.

**The sentence to leave with:** with the isolation level, the two databases
disagreed about *whether you were safe*. With the fix, they agree you are safe
and disagree only about *what it costs*. That is the difference between a
guarantee and a label.

## 10. The retry loop every level assumes you have

Every safe option in this episode — `SERIALIZABLE` on either engine, the
`WHERE`-guard, Episode 1's version column — hands failures back to you. None of
them is usable without a loop.

```python
for attempt in range(MAX_ATTEMPTS):
    try:
        async with conn.transaction():
            return await do_the_work(conn)
    except (asyncpg.SerializationError, asyncpg.DeadlockDetectedError):
        if attempt == MAX_ATTEMPTS - 1:
            raise
        await asyncio.sleep(random.uniform(0, 0.05 * 2 ** attempt))
```

Four things that are not optional:

1. **Catch the right exceptions.** SQLSTATE `40001` and `40P01` on Postgres,
   errno `1213` and `1205` on MySQL. Do not catch bare `Exception` — you will
   retry a constraint violation forever.
2. **Redo the whole transaction, including the side work.** If the price is part
   of the order and the order is redone, the price is redone. Hoisting it out of
   the loop makes the window narrower than the one you actually run.
3. **Jitter.** Two transactions that just conflicted are, by definition, running
   at the same time. A fixed backoff walks them straight back into each other.
4. **A cap, and a plan for exhausting it.** Giving up is a real outcome. In this
   capture it was 214 customers, and they need a real answer rather than a 500.

## 11. A cheat sheet for the level you are on

| You are on | Lost update? | Phantom insert? | What to do |
| --- | --- | --- | --- |
| Postgres `READ COMMITTED` | **yes** | **yes** | `WHERE`-guard, or one statement |
| Postgres `REPEATABLE READ` | no — aborts `40001` | **yes** | needs a retry loop; guard still simpler |
| Postgres `SERIALIZABLE` | no — aborts | no — aborts | correct; budget for the abort rate |
| MySQL `READ COMMITTED` | **yes** | **yes** | `WHERE`-guard, or one statement |
| MySQL `REPEATABLE READ` | **yes, silently** | no — deadlocks | the dangerous cell: no error to catch |
| MySQL `SERIALIZABLE` | no — deadlocks | no — deadlocks | correct; expect `1213`, not `40001` |

The row worth memorising is **MySQL at `REPEATABLE READ`** — the default — which
oversells with no error at all.

## 12. What to log

- **Rows affected on every guarded `UPDATE`.** `UPDATE 0` is the guard working;
  a handler that ignores it has all of the cost and none of the protection.
- **Serialization failures and deadlocks as their own counters**, by SQLSTATE and
  errno, never merged into one "db error" metric. The whole point of this episode
  is that the two engines report differently, and flattening the codes destroys
  the only signal that tells you which failure mode you are in.
- **Retry attempts per request, and exhaustions.** A rising retry count with a
  flat error rate is the shape of contention about to become an outage.
- **Time to make one sale, p50 and p99, split by outcome.** A rejection is fast
  by construction, so a percentile over everything measures how quickly you can
  say no.

## 13. Exercises

**1. Hide the bug.** Set `ISOLATION=serializable` and watch the oversell vanish
on both engines. Now look at what you bought: 279 aborts on Postgres, 288
deadlocks on MySQL, and no retry loop to catch either. Safe and unusable are not
the same result.

**2. Prove the code really is identical.** `curl -s localhost:8000/admin/sql`.
The only difference between the two renderings is the placeholder.

**3. Change the index and watch row C change engines.** Make
`reservations_sku_idx` unique and re-run the count-then-insert cell on MySQL. The
next-key lock becomes a record lock and the deadlocks change character. This is
the clearest demonstration in the repo that **the lock behaviour follows the
index, not the level.**

**4. Catch MySQL in the act.** Run the load with `ENGINE=mysql ISOLATION=repeatable-read`
and query `performance_schema.data_locks` in another terminal while it runs.
Everything is `GRANTED`. Nothing is waiting. Nothing is wrong, as far as the
engine is concerned.

**5. Write the retry loop.** Add one around the `SERIALIZABLE` cell and re-run
it. Measure how many attempts it takes to get 100 orders through, then decide
whether you would rather have that or the `WHERE` clause.

---

## Where to go next

Every answer here that was actually safe worked the same way underneath: it made
something **wait**, or it **killed** something. Next time the thing that gets
killed is a customer's checkout, and nobody wrote a line of wrong code —
[Episode 3](../episode-3-deadlock/), with the guide at
[`episode-3-deadlock/GUIDE.md`](../episode-3-deadlock/GUIDE.md).

- [Episode 1 — the phantom update](../episode-1-lost-update/GUIDE.md): where the
  handler in section 4 came from, and the three fixes it already has.
- Episode 4 — distributed locks: `sorted()` and `FOR UPDATE` both assume one
  database can see every lock. It cannot, once the critical section leaves it.

---

Part of the **System Sense — Database Concurrency** mini-series.
