# Episode 1 — The Phantom Update

**System Sense — Database Concurrency**, episode 1 of 4.
*A hundred seats. Three hundred sold. And the shelf says 88 are left.*

**▶ Watch the episode:** https://youtu.be/IU96-QIg7no · [full playlist](https://www.youtube.com/playlist?list=PLQlsUWTGdchk)

A runnable demo of the oldest bug in transactional software: reading a number,
changing it in your application, and writing it back. Clone it, run one command,
and watch a correct-looking checkout sell stock that does not exist.

```bash
docker compose up --build          # in one terminal
./scripts/capture-demo.sh          # in another — runs all four modes, writes capture/metrics.json
```

> **[GUIDE.md](GUIDE.md)** is the written companion: the same ground more
> slowly, plus what would not fit in eleven minutes — what an ORM emits for
> `item.stock -= 1`, how to choose between the four fixes, how to find this in
> a codebase you did not write, and what to log so it is not invisible.


## The one table to remember

One hundred units of one SKU. Three hundred customers, twenty-five in flight at
a time, one unit each. The same load four times; the only variable is
`ORDER_MODE`.

| | `naive` | `atomic` | `pessimistic` | `optimistic` |
| --- | --- | --- | --- | --- |
| Orders confirmed | **300** | 100 | 100 | 86 |
| Stock left on the shelf | **88** | 0 | 0 | 14 |
| **Units oversold** | **200** | 0 | 0 | 0 |
| Customers turned away | 0 | 200 sold out | 200 sold out | 214 conflict |
| `CHECK (stock >= 0)` violations | 0 | 0 | 0 | 0 |
| Time to make one sale, p50 | 196 ms | 183 ms | **4,688 ms** | 187 ms |
| Orders per second | 114.1 | **122.8** | 15.6 | 27.4 |
| Retries | — | — | — | **1,161** |

Read the first two rows together, because that is the whole episode. Three
hundred people bought one of a hundred seats, every one of them was confirmed,
nobody saw an error, and the database is still offering 88 more.

Every figure here comes from `capture/metrics.json`, produced by
`./scripts/capture-demo.sh` in this folder. Nothing is estimated.

## The four modes

All four live in [`app/main.py`](app/main.py), one function each.

- **`naive`** — `SELECT` the stock, subtract in Python, `UPDATE` the literal
  back. There is no bug in any single line of it, and it is close to what
  SQLAlchemy or Django emits for `item.stock -= 1`.
- **`atomic`** — `UPDATE inventory SET stock = stock - $2 WHERE sku_id = $1 AND
  stock >= $2`. One statement, so there is no "between". Zero rows returned is
  the database telling you it is sold out.
- **`pessimistic`** — `SELECT … FOR UPDATE`, then the same read-modify-write.
  Correct, and everybody queues behind one row for the whole pricing call.
- **`optimistic`** — a version column and a retry loop. Correct, and **not
  free**: 1,161 retries, 214 customers refused after five attempts each, and 14
  units left unsold on a shelf that had them.

## The constraint that never fires

`db/init.sql` puts `CHECK (stock >= 0)` on the table, because that is what a
reviewer asks for. It fired **0** times. A lost update does not write a negative
number; it writes a plausible one. Nor is there anything to `GROUP BY` — the
order count is right and a column is wrong.

## The knob

```bash
ORDER_MODE=atomic docker compose up --build
```

or switch it live, which is what the capture script does:

```bash
curl -X POST localhost:8000/admin/mode -H 'content-type: application/json' -d '{"mode":"atomic"}'
curl -X POST 'localhost:8000/admin/reset?sku_stock=100'
python3 scripts/order.py --orders 300 --concurrency 25
curl -s localhost:8000/api/inventory/1
```

**Try this — hide the bug without fixing it.** Narrow the race window:

```bash
PRICING_BASE_MS=1 PRICING_SPREAD_MS=2 docker compose up --build
```

The oversell collapses and not one line of the handler changed. Fewer requests
land inside a shorter window. That is why this passes on a laptop, passes in
staging, and sells 200 seats that do not exist on the day of the ticket drop.

Running the app with a single worker and a pool size of one does the same thing
for the same reason.

## What is where

```
app/main.py         the four handlers, one function each
app/config.py       the mode knob
pricing/main.py     the race window: a real service call, deterministic latency by id
db/init.sql         inventory + orders, and the CHECK that never fires
scripts/order.py    the customers — standard library only, no dependencies
scripts/capture-demo.sh   runs all four modes and writes capture/metrics.json
capture/            the measured output every number above comes from
```

The pricing call takes `60 + (sku_id * 137 + customer_id * 31) % 240` ms, which
is 60 to 299 ms. It is a **deterministic function of the ids on purpose**: a
random sleep would give a different answer every run, and a `sleep` in the
handler would make the oversell a setting rather than a measurement. Measured
across the naive run, the gap between the `SELECT` and the `UPDATE` was 185.2 ms
at the median, 62.9 minimum, 356.1 maximum.

Counts move by about one between runs — two runs on the same machine gave a
shelf of 87 and then 88. The conclusions do not move.

---

Next: **Episode 2 — Transaction Isolation is a Lie**, where the setting that
claims to fix all of this turns out to mean two different things on two
different databases.

Part of the **System Sense — Database Concurrency** mini-series.
