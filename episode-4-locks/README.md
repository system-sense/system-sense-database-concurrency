# Episode 4 — Distributed Locks Without Disaster

**System Sense — Database Concurrency**, episode 4 of 4.
*The lock expired while the worker still believed it held it.*

This folder is **Episode 3's app**, extended in place. The checkout, the basket,
the sorted lock order and both engines are all still here and still work; what
is new is a critical section that a row lock cannot protect.

The three episodes before this one all ended the same way. Something waited, or
something died — and every one of those answers assumed **one database could see
every lock**. Here the decision spans a call to somebody else's system: read the
stock, decide to allocate the last unit, dispatch a parcel, then write. Hold
`SELECT ... FOR UPDATE` across that and you drain the connection pool. So the
lock leaves the database, and the moment it does it stops being a lock and
becomes a **lease**.

Nothing tells the holder when the lease ran out.

```bash
docker compose up --build          # in one terminal
./scripts/capture-demo.sh          # in another
```

## The knob

`LOCK`, and `LOCK_TTL_MS` beside it.

| `LOCK` | What it is |
| --- | --- |
| `none` | The control. Two workers, one unit, two parcels. |
| `redis` | A textbook single-node lock, hygienically written. It still oversells. |
| `redlock` | The quorum, and a node restart that forgets what it granted. |
| `advisory` | `pg_advisory_xact_lock` — no TTL to expire, because it dies with the session. |
| `fenced` | The lock is still lost. The storage layer refuses the stale write anyway. |

## The numbers

Measured by `./scripts/capture-demo.sh`, from **80 units on 8 shelves, 300
customers, 25 in flight**, and a **1000 ms lease** against a critical section
whose median is 994 ms. The lease sits below the median, so which workers
overrun is decided by arithmetic on the ids rather than by a sleep.

| `LOCK` | parcels | sold | no sale | leases expired | writes refused |
| --- | --- | --- | --- | --- | --- |
| `none` | 293 | 80 | 213 | 0 | 0 |
| `redis` | 114 | 80 | 34 | 52 | 0 |
| `redlock` | 113 | 80 | 33 | 52 | 0 |
| `advisory` | 80 | 80 | 0 | 0 | 0 |
| `fenced` | 161 | 80 | 81 | 84 | 81 |
| `redis` at `LOCK_TTL_MS=2600` | 80 | 80 | 0 | 0 | 0 |

Read the last two rows together, because they are the episode:

- **`fenced` gets the shelf exactly right and makes the parcel count worse** —
  81 parcels with nothing sold behind them, against `redis`'s 34. A refused
  write leaves the stock undecremented, so the next worker finds stock and
  dispatches. Fencing moves the damage out of your data and into your loading
  bay. The write is refused; the van has already gone.
- **Raising the lease to 2600 ms takes every one of those numbers to zero, and
  fixes nothing.** The window is narrower. That is the exercise.

Every figure traces to `capture/metrics.json`. Re-run it and the counts move by
a few, because it is a real race; the split between the modes does not move.
