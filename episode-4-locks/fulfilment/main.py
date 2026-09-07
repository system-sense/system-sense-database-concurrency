"""The fulfilment service. It exists to take a realistic amount of time AND to
leave something behind that cannot be rolled back.

Episode 3's pricing call also sat between a read and a write, and the
difference between the two is the whole reason this episode exists. Pricing is
a QUESTION: ask it twice and nothing has happened. Fulfilment is a DECISION
somebody else acts on. Once this returns a consignment id, a parcel is on a
van, and no ROLLBACK, no lock and no fencing token recalls it.

That is why the critical section here cannot be a row lock. `SELECT ... FOR
UPDATE` held across a multi-second HTTP call is measured in this episode rather
than dismissed, because it is the right answer whenever the work stays inside
the database -- but it drains the connection pool the moment the work does not.

The latency is a DETERMINISTIC function of the ids, as everywhere in this
series:

    FULFILMENT_BASE_MS + (sku_id * 89 + worker_id * 53) % FULFILMENT_SPREAD_MS

With the defaults that spans 400-1599 ms, which straddles the default
LOCK_TTL_MS of 1000. That is the episode's headline number and it is emergent:
some workers finish inside their lease and some do not, decided by arithmetic
on the ids rather than by a sleep somebody tuned until the demo worked.

Raise LOCK_TTL_MS above the p99 and the oversell vanishes. Nothing has been
fixed. A stop-the-world GC pause makes the window wide again, and not one line
of the application changed.
"""
import asyncio
import itertools
import os

from fastapi import FastAPI

BASE_MS = int(os.getenv("FULFILMENT_BASE_MS", "400"))
SPREAD_MS = int(os.getenv("FULFILMENT_SPREAD_MS", "1200"))

app = FastAPI(title="System Sense — fulfilment")

# Consignments are counted, not stored: the point is only that a real one was
# handed out and cannot be taken back.
_consignments = itertools.count(1)


def latency_ms(sku_id: int, worker_id: int) -> int:
    return BASE_MS + (sku_id * 89 + worker_id * 53) % SPREAD_MS


@app.get("/health")
async def health():
    return {"ok": True, "base_ms": BASE_MS, "spread_ms": SPREAD_MS}


@app.post("/dispatch")
async def dispatch(sku_id: int, worker_id: int, qty: int = 1):
    """Dispatch a parcel. There is no undo, and that is the point."""
    took = latency_ms(sku_id, worker_id)
    await asyncio.sleep(took / 1000)
    return {
        "sku_id": sku_id,
        "worker_id": worker_id,
        "qty": qty,
        "consignment_id": next(_consignments),
        "took_ms": took,
        # Said out loud in the payload because the episode says it out loud on
        # the board: this is the line no lock in the series can protect.
        "recallable": False,
    }
