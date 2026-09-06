# System Sense — Database Concurrency

The runnable demos for a four-part series on what your database actually does
when two requests arrive at the same moment, and what it quietly does not do
for you.

**▶ Playlist:** https://www.youtube.com/playlist?list=PLQlsUWTGdchk

Every episode is a folder. Each one runs with a single command, and every
number the videos put on screen is measured from a real run in that folder,
never estimated: `./scripts/capture-demo.sh` produces `capture/metrics.json`
and the videos read it.

| Episode | Folder | What it measures |
| --- | --- | --- |
| 1 — The Phantom Update | [`episode-1-lost-update/`](episode-1-lost-update/) | Read-then-write. 100 in stock, 300 sold, 88 still on the shelf. |
| 2 — Transaction Isolation is a Lie | [`episode-2-isolation/`](episode-2-isolation/) | The same code at REPEATABLE READ on Postgres and MySQL, and they disagree. |
| 3 — Deadlocks You Did Not Write | `episode-3-deadlock/` | Not started. |
| 4 — Distributed Locks Without Disaster | `episode-4-locks/` | Not started. |

```bash
cd episode-1-lost-update
docker compose up --build      # in one terminal
./scripts/capture-demo.sh      # in another
```

No cloud account, no API keys, no manual seeding. Each episode extends the
previous one's app in place, so `git log` shows what each one adds as a diff
rather than a rewrite.

Watch: https://youtu.be/IU96-QIg7no
