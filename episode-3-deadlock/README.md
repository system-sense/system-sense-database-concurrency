# Episode 2 — Transaction Isolation Is a Lie

**System Sense — Database Concurrency**, episode 2 of 4.
*One level name. Two databases. They swap which one is safe.*

Episode 1 ended on a setting: raise the isolation level, because that is what it
is for. This folder turns it on, runs the same statements against **PostgreSQL 16
and MySQL 8**, and lets them disagree.

```bash
docker compose up --build          # in one terminal
./scripts/capture-demo.sh          # in another — runs all ten cells, writes capture/metrics.json
```

## The matrix

One hundred units of one SKU. Three hundred customers, twenty-five in flight,
one seat each. The same load ten times. **The application code never branches on
the engine** — the only difference is `$1` against `%s`, and `/admin/sql` prints
both renderings so you can check that rather than believe it.

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

Read the four bold rows as two pairs, because that is the whole episode.

**At `REPEATABLE READ`, on the lost update, Postgres is safe.** It refuses 280
writes with SQLSTATE `40001` and oversells nothing. MySQL, the same level, the
same code, confirms all three hundred orders, oversells two hundred seats, and
**raises no error at all**.

**At `REPEATABLE READ`, on the phantom insert, they trade places.** Postgres
commits everything and oversells 24 — `FOR SHARE` locks the rows that exist, and
Postgres has no gap locks. MySQL's next-key locking turns identical code into
295 deadlocks, errno `1213`, and oversells nothing.

Neither vendor's `REPEATABLE READ` is the other's. The level name is not a
guarantee; it is a label on a table of anomalies the standard defines by what
they forbid, not by how they work.

Every figure here comes from `capture/metrics.json`. Nothing is estimated.

## The three scenarios

All three are in [`app/main.py`](app/main.py), written **once**, against the seam
in [`app/engines.py`](app/engines.py). None of them mentions Postgres or MySQL.

- **`read_modify_write`** — Episode 1's bug. `SELECT` the stock, subtract in
  Python, `UPDATE` the literal back.
- **`count_then_insert`** — count the reservations, decide there is room, insert
  one. Nothing is updated, so nothing conflicts: every write is kept and the
  *invariant* is what breaks. The `FOR SHARE` is deliberate — it is the
  strongest lock you can take on rows you are only reading, and on Postgres it
  still locks nothing but the rows that are already there.
- **`where_guard`** — the portable fix: put the value you read into the `WHERE`
  clause, and retry when zero rows come back.

## The landing, and what it costs on each engine

The fix that works is not a level. `WHERE stock = <the value I read>` is a
current read on both engines, needs no setting, and is Episode 1's optimistic
mode. Both engines oversell **nothing**. But they charge for it differently:

| | Postgres | MySQL |
| --- | --- | --- |
| Booked, of 100 | 86 | **100** |
| Oversold | 0 | 0 |
| Customers refused after 5 attempts | **214** | 0 |
| Retries | **1,186** | 133 |
| Time to make one sale, p50 | 219 ms | **4,785 ms** |

Postgres pays in **customers turned away**. MySQL pays in **waiting** — and
4,785 ms is Episode 1's `pessimistic` mode (4,688 ms) reappearing without anyone
choosing it. `capture/locks-my-rc-guard.log` says why, sampled while the load was
running:

```
inventory  NULL     TABLE   IX             GRANTED  24
inventory  PRIMARY  RECORD  X,REC_NOT_GAP  WAITING  23
inventory  PRIMARY  RECORD  X,REC_NOT_GAP  GRANTED   1
```

One holder, twenty-three queued behind it. The matching Postgres sample has
**zero** ungranted locks: nobody waits, so the losers spin and retry instead.

That Postgres column is also Episode 1's `optimistic` mode, reproduced by a
different statement in a different episode: Episode 1 measured 86 booked, 1,161
retries and 214 customers abandoned; this measured 86, 1,186 and 214.

## Looking at the locks yourself

They only exist while a load is running — once the last transaction commits,
both views are empty. That is the whole difficulty of showing anyone a lock. So
start a load in one terminal and look in another:

```bash
python3 scripts/order.py --orders 300 --concurrency 25
```

```bash
# MySQL — the gap locks, named by the engine
docker compose exec mysql mysql -usysense -psysense sysense -e \
  "SELECT OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE, LOCK_STATUS, count(*)
     FROM performance_schema.data_locks GROUP BY 1,2,3,4,5;"

# Postgres — and count what is waiting, which is the interesting column
docker compose exec postgres psql -U sysense -d sysense -c \
  "SELECT locktype, mode, granted, count(*) FROM pg_locks GROUP BY 1,2,3 ORDER BY 4 DESC;"
```

On `count_then_insert` at `REPEATABLE READ`, MySQL shows
`X,INSERT_INTENTION … WAITING` on `reservations_sku_idx`. That is the gap lock.
Postgres, on identical code, shows nothing waiting at all.

The index it is taken on is **non-unique on purpose**, and the capture reads that
back off the engine (`SHOW INDEX`, `Non_unique=1`) rather than assuming it,
because InnoDB's next-key locking is what the whole row turns on.

## The knob

```bash
ISOLATION=serializable docker compose up --build
```

or switch it live, which is what the capture script does:

```bash
curl -X POST localhost:8000/admin/config -H 'content-type: application/json' \
  -d '{"engine":"mysql","isolation":"repeatable-read","scenario":"read_modify_write"}'
curl -X POST 'localhost:8000/admin/reset?sku_stock=100'
python3 scripts/order.py --orders 300 --concurrency 25
curl -s localhost:8000/api/state
```

**Try this — hide the bug without fixing it.** Set `ISOLATION=serializable`. The
oversell vanishes on both engines. Now look at what you bought: Postgres aborts
279 of 300 transactions and MySQL deadlocks 288 of them, and **your application
has no retry loop to catch either**. Safe and unusable are not the same result,
and the difference does not show up until the day the traffic does.

## What is where

```
app/main.py         the three scenarios, written once, engine-neutral
app/engines.py      the seam: `?` rendered as `$1` or `%s`, and nothing else
app/config.py       the knobs — engine, isolation, scenario
db/init.sql         Postgres schema
db/init.mysql.sql   the same schema in MySQL, differences forced by the dialect only
pricing/main.py     the race window: a real service call, deterministic latency by id
scripts/order.py    the customers — standard library only, no dependencies
scripts/capture-demo.sh   the ten cells, then the lock evidence, then metrics.json
capture/            the measured output every number above comes from
capture/locks-*.log lock views sampled DURING the load, on a separate pass
```

The lock evidence is gathered on its own load rather than during the matrix, on
purpose: polling two engines over `docker compose exec` costs a few hundred
milliseconds a sample, and the matrix is timing sales to the millisecond.

Counts move by a few between runs — the WHERE-guard cell booked 87, 88, 86 and 86
on four runs of the same machine, and MySQL booked 100 on all four. No conclusion
moves.

---

Previous: **[Episode 1 — The Phantom Update](../episode-1-lost-update/)**, where
three hundred people bought one of a hundred seats and the shelf still said 88.

Next: **Episode 3 — The 2-Second Deadlock**. Every answer here that was actually
safe worked by making something wait, or by killing it. Next time the thing that
gets killed is a customer's checkout, and nobody wrote a line of wrong code.

Part of the **System Sense — Database Concurrency** mini-series ·
[playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)
