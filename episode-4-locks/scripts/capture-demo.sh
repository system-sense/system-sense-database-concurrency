#!/usr/bin/env bash
#
# Runs the Episode 4 matrix and records what actually happened.
#
#   ./scripts/capture-demo.sh
#
# The episode's claim is that a lock with a timeout is a lease, that nothing
# tells the holder when the lease ran out, and that no amount of lock hygiene
# fixes it -- only the storage layer can. So every cell runs the SAME handler
# and the same load, and only LOCK moves.
#
# Two kinds of run, and the difference is stated everywhere it appears:
#
#   THE FLEET      unattended, 300 workers, and the source of every number the
#                  episode quotes. Whether a worker outlives its lease is a
#                  function of the ids, so the expiries are emergent.
#   THE FORENSICS  one request on each of two workers, with one of them frozen
#                  by `docker compose pause`. It shows the MECHANISM and it
#                  produces no figure the narration quotes.
#
# Writes:  capture/*.log  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

RESUME=${RESUME:-0}

ORDERS=${ORDERS:-300}
CONCURRENCY=${CONCURRENCY:-25}
# Eight shelves and a small number of units on each. The stock has to be SMALL
# relative to the fleet or nothing contends: 300 workers against 800 units, as
# in Episode 3, would have every worker find stock and no two of them ever race
# for the last one.
STOCK=${STOCK:-10}
TTL=${TTL:-1000}

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()   { docker compose "$@"; }
psql() { dc exec -T postgres psql -U sysense -d sysense -At -F' ' "$@"; }

wait_healthy() {
  printf 'waiting for the stack '
  for _ in $(seq 1 150); do
    if curl -fsS localhost:8000/health  >/dev/null 2>&1 &&
       curl -fsS localhost:8001/health  >/dev/null 2>&1 &&
       curl -fsS localhost:9100/health  >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app worker-b | tail -40; return 1
}

# One cell: lock mode x lease.
#
# The config is asserted rather than assumed. Attributing an advisory run's zero
# to the lock when a POST had silently failed would be a very quiet way to
# publish a wrong episode.
cell() {
  local mode=$1 ttl=$2 tag=$3
  local got

  if [ "$RESUME" = "1" ] && grep -q "^STATS $tag " "$OUT/cell-$tag.log" 2>/dev/null; then
    log "$tag  -- already measured, skipping"
    return 0
  fi

  for port in 8000 8001; do
    got=$(curl -fsS -X POST "localhost:$port/admin/config" -H 'content-type: application/json' \
          -d "{\"engine\":\"postgres\",\"scenario\":\"allocate_and_dispatch\",\"lock\":\"$mode\",\"lock_ttl_ms\":$ttl}" \
          | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["lock"] + "/" + str(d["lock_ttl_ms"]) + "/" + d["scenario"])')
    [ "$got" = "$mode/$ttl/allocate_and_dispatch" ] || {
      echo "config did not take on :$port -- wanted $mode/$ttl/allocate_and_dispatch, got $got"; exit 1; }
  done

  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null

  {
    echo "CELL $tag lock=$mode ttl_ms=$ttl stock_per_sku=$STOCK"
    python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" \
      --spread-skus --max-basket-items 1 --label "$tag"
    echo "STATE $tag $(curl -fsS localhost:8000/api/state)"
    echo "STATS $tag $(curl -fsS localhost:8000/admin/stats)"
  } 2>&1 | tee "$OUT/cell-$tag.log"
}

if [ "$RESUME" = "1" ]; then
  log "1/7  Resuming -- keeping the stack and any cells already measured"
else
  log "1/7  Tearing down any previous run"
  dc down -v --remove-orphans >/dev/null 2>&1 || true
fi

log "2/7  Starting the stack"
dc up --build -d 2>&1 | tee -a "$OUT/01-compose-up.log"
wait_healthy
{
  curl -fsS localhost:8000/api/versions
  echo
  echo "orders=$ORDERS concurrency=$CONCURRENCY stock_per_sku=$STOCK lease_ms=$TTL"
} >> "$OUT/01-compose-up.log"

log "3/7  The settings this episode measures, read off the services"
{
  echo "-- the lease, as the app is running it"
  curl -fsS localhost:8000/admin/stats | python3 -c 'import json,sys; d=json.load(sys.stdin); print("lock_ttl_ms", d["lock"]["ttl_ms"])'
  echo
  echo "-- the critical section's configured latency, read off fulfilment"
  curl -fsS localhost:9100/health
  echo
  echo "-- redis persistence: OFF, which is what makes a restart forget"
  dc exec -T redis-a redis-cli CONFIG GET save
  dc exec -T redis-a redis-cli CONFIG GET appendonly
  echo
  echo "-- postgres advisory locks live in the normal lock manager"
  psql -c "SELECT locktype, mode FROM pg_locks WHERE locktype = 'advisory' LIMIT 5;"
} 2>&1 | tee "$OUT/02-settings.log"

log "4/7  The fleet -- every number the episode quotes comes from here"
#    mode      ttl    tag
# The control. No lock at all, so the episode can show the critical section
# genuinely needs protecting before arguing about which protection to use.
cell none      "$TTL" none
# A textbook single-node lock, written hygienically. It still oversells.
cell redis     "$TTL" redis
# The quorum. Its own cell, before the node restart below breaks it.
cell redlock   "$TTL" redlock
# No TTL to expire, and a pool connection pinned across the external call.
cell advisory  "$TTL" advisory
# The lock is still lost; the storage layer refuses the stale write anyway.
cell fenced    "$TTL" fenced
# The hide-the-bug exercise: raise the lease above the p99 critical section.
# The oversell vanishes and not one line of the application changed.
cell redis     2600   redis-long-lease

log "5/7  The forensic pause -- mechanism only, quoted by nothing"
# Two workers, one unit, single stepped. worker-b takes the lock and is FROZEN
# mid-dispatch by the cgroup freezer, which is what a stop-the-world GC pause
# looks like from outside the process. It cannot renew. Its lease expires. The
# other worker takes the lock it still believes it holds.
forensics() {
  local mode=$1
  for port in 8000 8001; do
    curl -fsS -X POST "localhost:$port/admin/config" -H 'content-type: application/json' \
      -d "{\"engine\":\"postgres\",\"scenario\":\"allocate_and_dispatch\",\"lock\":\"$mode\",\"lock_ttl_ms\":$TTL}" >/dev/null
  done
  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=1" >/dev/null

  echo "-- LOCK=$mode, lease ${TTL}ms, one unit of stock on sku 1"
  echo "-- worker-b acquires, then is frozen mid-dispatch"
  curl -fsS -X POST localhost:8001/api/orders -H 'content-type: application/json' \
    -d '{"sku_id":1,"customer_id":901,"qty":1}' > "$OUT/.forensic-b.json" 2>&1 &
  local bpid=$!
  sleep 0.35
  dc pause worker-b >/dev/null
  echo "-- frozen. waiting out the lease."
  sleep $(python3 -c "print($TTL/1000 + 1.2)")
  echo "-- the lock it thinks it holds, as redis sees it now:"
  dc exec -T redis redis-cli GET lock:sku:1 || true
  echo "-- worker A now takes that lock and sells the same unit:"
  curl -fsS -X POST localhost:8000/api/orders -H 'content-type: application/json' \
    -d '{"sku_id":1,"customer_id":902,"qty":1}' || true
  echo
  echo "-- unfreezing worker-b. It has no idea any time passed."
  dc unpause worker-b >/dev/null
  wait $bpid || true
  echo "-- worker-b's own result: $(cat "$OUT/.forensic-b.json")"
  echo "-- the shelf, and the row's fence token:"
  psql -c "SELECT sku_id, stock, fence_token FROM inventory WHERE sku_id = 1;"
  echo "-- parcels dispatched across both workers:"
  for port in 8000 8001; do
    curl -fsS "localhost:$port/admin/stats" | python3 -c 'import json, sys
d = json.load(sys.stdin)["lock"]
print("   :%s dispatched=%s lease_expired=%s fenced_out=%s"
      % (sys.argv[1], d["dispatched"], d["lease_expired"], d["fenced_out"]))' "$port"
  done
  rm -f "$OUT/.forensic-b.json"
}
{
  echo "== FORENSIC ONLY. These counts are NOT the episode's numbers. =="
  echo
  forensics redis
  echo
  echo "== the same freeze, with fencing on =="
  forensics fenced
} 2>&1 | tee "$OUT/06-forensic-pause.log"

log "6/7  Redlock, and the node that forgets"
# Measured rather than argued. Persistence is off on all three nodes, so a
# restart genuinely loses what that node granted -- and a quorum built on a
# node that has forgotten can hand out a lock somebody already holds.
{
  echo "== Redlock's crash-restart, single stepped =="
  for port in 8000 8001; do
    curl -fsS -X POST "localhost:$port/admin/config" -H 'content-type: application/json' \
      -d "{\"engine\":\"postgres\",\"scenario\":\"allocate_and_dispatch\",\"lock\":\"redlock\",\"lock_ttl_ms\":20000}" >/dev/null
  done
  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=1" >/dev/null

  echo "-- worker-b takes the lock across all three nodes"
  curl -fsS -X POST localhost:8001/api/orders -H 'content-type: application/json' \
    -d '{"sku_id":1,"customer_id":911,"qty":1}' > /dev/null 2>&1 &
  sleep 0.3
  for n in redis-a redis-b redis-c; do
    printf '   %s holds: ' "$n"; dc exec -T "$n" redis-cli GET lock:sku:1 || true
  done
  echo "-- restarting redis-a. No persistence, so it comes back empty."
  dc restart redis-a >/dev/null
  sleep 2
  for n in redis-a redis-b redis-c; do
    printf '   %s holds: ' "$n"; dc exec -T "$n" redis-cli GET lock:sku:1 || true
  done
  echo "-- a majority is now reachable that has NOT got the lock recorded."
  echo "-- antirez's rebuttal is fair and the episode gives it: delayed restarts"
  echo "-- fix this, and Redlock never claimed to survive a node lying about its"
  echo "-- state. The point is that three Redises is not a free upgrade."
  wait || true
} 2>&1 | tee "$OUT/07-redlock-restart.log"

log "7/7  Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
