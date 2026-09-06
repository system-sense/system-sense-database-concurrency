# System Sense — Database Concurrency

The runnable demos for a four-part series on what your database actually does
when two requests arrive at the same moment, and what it quietly does not do
for you.

**▶ Playlist:** https://www.youtube.com/playlist?list=PLQlsUWTGdchk

Every episode is a folder. Each one runs with a single command, and every
number the videos put on screen is measured from a real run in that folder,
never estimated: `./scripts/capture-demo.sh` produces `capture/metrics.json`
and the videos read it.

| Episode | Folder | What it measures | |
| --- | --- | --- | --- |
| 1 — The Phantom Update | [`episode-1-lost-update/`](episode-1-lost-update/) | Read-then-write. 100 in stock, 300 sold, 88 still on the shelf. | [**watch**](https://youtu.be/IU96-QIg7no) · [guide](episode-1-lost-update/GUIDE.md) |
| 2 — Transaction Isolation Is a Lie | [`episode-2-isolation/`](episode-2-isolation/) | The same statements at `REPEATABLE READ` on Postgres and MySQL, and they swap which one is safe. | [**watch**](https://youtu.be/ZAnJ6O1rVDk) · [guide](episode-2-isolation/GUIDE.md) |
| 3 — The Deadlock | [`episode-3-deadlock/`](episode-3-deadlock/) | Correct locks taken in the order the customer's basket happened to be in. 159 turned away, 446 units on the shelf. | [guide](episode-3-deadlock/GUIDE.md) |
| 4 — Distributed Locks Without Disaster | `episode-4-locks/` | Not started. | |

```bash
cd episode-1-lost-update
docker compose up --build      # in one terminal
./scripts/capture-demo.sh      # in another
```

No cloud account, no API keys, no manual seeding. Each episode extends the
previous one's app in place, so `git log` shows what each one adds as a diff
rather than a rewrite.

## The written guides

Every episode has a **`GUIDE.md`** beside its demo — a written companion that
covers the same ground more slowly, with the code in full, and then goes on into
what would not fit in the runtime. They are for reading rather than watching,
and they are where the operational detail lives.

| Guide | What it adds beyond the video |
| --- | --- |
| [1 — The Gap Between the Read and the Write](episode-1-lost-update/GUIDE.md) | what an ORM emits for `item.stock -= 1` · choosing between the four fixes · finding this in a codebase you did not write · what to log |
| [2 — One Level Name, Two Different Guarantees](episode-2-isolation/GUIDE.md) | what the SQL standard actually defines · reading a lock view mid-load · a cheat sheet for the level you are on · the retry loop every level assumes |
| [3 — Nobody Chose the Order](episode-3-deadlock/GUIDE.md) | reading a Postgres deadlock report line by line · why `deadlock_timeout` is a full second · `lock_timeout` versus a retry · lock ordering across tables |
