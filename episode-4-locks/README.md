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

## Status

**Phase 1 — the demo is being built and nothing here has been measured yet.**
There is no `capture/metrics.json` in this folder, and until there is, no number
about this episode exists. That is deliberate: in this series the narration is
written against the capture, never the other way round.
