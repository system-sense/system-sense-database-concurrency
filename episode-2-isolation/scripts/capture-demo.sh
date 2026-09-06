#!/usr/bin/env bash
#
# Runs the Episode 2 matrix and records what actually happened.
#
#   ./scripts/capture-demo.sh
#
# The episode's claim is that identical code at an identically named isolation
# level behaves differently on two engines. So every cell is run against BOTH,
# back to back, from the same load generator, in the same run. A result from one
# engine alone proves nothing here.
#
# Writes:  capture/*.log  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

# RESUME=1 keeps the stack and any cells already measured, and re-runs only what
# is missing. The matrix is ten cells across two engines and a kill partway
# through used to cost all of them; each cell resets its own state before it
# runs, so skipping finished ones is safe.
RESUME=${RESUME:-0}

ORDERS=${ORDERS:-300}
CONCURRENCY=${CONCURRENCY:-25}
STOCK=${STOCK:-100}

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()   { docker compose "$@"; }
psql() { dc exec -T postgres psql -U sysense -d sysense -At -F' ' "$@"; }
mysql_() { dc exec -T mysql mysql -usysense -psysense sysense -N -B "$@" 2>/dev/null; }

wait_healthy() {
  printf 'waiting for the stack '
  for _ in $(seq 1 120); do
    if curl -fsS localhost:8000/health >/dev/null 2>&1 &&
       curl -fsS localhost:9000/health >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app mysql | tail -40; return 1
}

# One cell of the matrix: engine x isolation x scenario.
#
# The config is asserted rather than assumed. Attributing MySQL's numbers to
# Postgres because a POST silently failed would be a very quiet way to publish
# a wrong episode, and this episode is entirely a claim about which engine did
# what.
cell() {
  local engine=$1 isolation=$2 scenario=$3 tag=$4
  local got

  # A cell is finished when its log carries the STATS line, which is written
  # last. A half-written log from a killed run is re-run rather than trusted.
  if [ "$RESUME" = "1" ] && grep -q "^STATS $tag " "$OUT/cell-$tag.log" 2>/dev/null; then
    log "$tag  -- already measured, skipping"
    return 0
  fi
  got=$(curl -fsS -X POST localhost:8000/admin/config -H 'content-type: application/json' \
        -d "{\"engine\":\"$engine\",\"isolation\":\"$isolation\",\"scenario\":\"$scenario\"}" \
        | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d["engine"] + "/" + d["isolation"] + "/" + d["scenario"])')
  [ "$got" = "$engine/$isolation/$scenario" ] || { echo "config did not take: wanted $engine/$isolation/$scenario, got $got"; exit 1; }

  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null

  {
    echo "CELL $tag engine=$engine isolation=$isolation scenario=$scenario"
    python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" --label "$tag"
    echo "STATE $tag $(curl -fsS localhost:8000/api/state)"
    echo "STATS $tag $(curl -fsS localhost:8000/admin/stats)"
  } 2>&1 | tee "$OUT/cell-$tag.log"
}

if [ "$RESUME" = "1" ]; then
  log "1/6  Resuming -- keeping the stack and any cells already measured"
else
  log "1/6  Tearing down any previous run"
  dc down -v --remove-orphans >/dev/null 2>&1 || true
fi

log "2/6  Starting the stack (postgres + mysql + pricing + app)"
dc up --build -d 2>&1 | tee -a "$OUT/01-compose-up.log"
wait_healthy
{
  curl -fsS localhost:8000/api/versions
  echo
  echo "orders=$ORDERS concurrency=$CONCURRENCY stock=$STOCK"
} >> "$OUT/01-compose-up.log"

log "3/6  The same statements, as each engine receives them"
curl -fsS localhost:8000/admin/sql | python3 -m json.tool | tee "$OUT/02-statements.log"

log "4/6  The matrix"
#      engine    isolation        scenario           tag
cell postgres read-committed  read_modify_write  pg-rc-rmw
cell mysql    read-committed  read_modify_write  my-rc-rmw
cell postgres repeatable-read read_modify_write  pg-rr-rmw
cell mysql    repeatable-read read_modify_write  my-rr-rmw
cell postgres repeatable-read count_then_insert  pg-rr-cti
cell mysql    repeatable-read count_then_insert  my-rr-cti
cell postgres serializable    read_modify_write  pg-ser-rmw
cell mysql    serializable    read_modify_write  my-ser-rmw
cell postgres read-committed  where_guard        pg-rc-guard
cell mysql    read-committed  where_guard        my-rc-guard

log "5/6  What the engines say about their own locks"
{
  echo "-- mysql: the index the next-key locks are taken on."
  echo "-- Non_unique=1 is the load-bearing fact: read off the engine, not assumed."
  mysql_ -e "SHOW INDEX FROM reservations;" || true
  echo
  echo "-- mysql: deadlocks recorded during this run"
  dc logs mysql 2>&1 | grep -c "TRANSACTION" || true
} 2>&1 | tee "$OUT/03-locks.log"

# The locks themselves have to be caught WHILE the load is on: once the last
# transaction commits there is nothing left in either view to look at.
#
# This is a SEPARATE pass, and deliberately so. Polling two engines over
# `docker compose exec` costs a few hundred milliseconds a sample, and the
# matrix above is measuring latency to the millisecond -- sampling inside those
# cells would mean publishing numbers taken from a system that was being probed
# while it was timed. So the matrix runs clean, and the lock evidence is
# gathered afterwards on its own load.
#
# THESE RUNS PRODUCE NO NUMBERS THE EPISODE QUOTES. Only lock modes, which are
# qualitative: whether InnoDB takes a gap lock where Postgres takes none, and
# whether the WHERE-guard queues or spins. Both are claims the episode makes
# about somebody else's database, so neither may come from documentation.
lock_evidence() {
  local engine=$1 isolation=$2 scenario=$3 tag=$4
  curl -fsS -X POST localhost:8000/admin/config -H 'content-type: application/json' \
    -d "{\"engine\":\"$engine\",\"isolation\":\"$isolation\",\"scenario\":\"$scenario\"}" >/dev/null
  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null

  {
    echo "LOCKS $tag engine=$engine isolation=$isolation scenario=$scenario"
    echo "-- evidence only: the counts from this load are NOT the episode's numbers"
    for i in $(seq 1 60); do
      echo "-- sample $i --"
      if [ "$engine" = "postgres" ]; then
        psql -c "SELECT locktype, mode, granted, count(*) FROM pg_locks GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 6;" || true
        psql -c "SELECT count(*) FROM pg_locks WHERE NOT granted;" || true
      else
        mysql_ -e "SELECT OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE, LOCK_STATUS, count(*) FROM performance_schema.data_locks GROUP BY 1,2,3,4,5 ORDER BY 6 DESC LIMIT 8;" || true
        mysql_ -e "SELECT count(*) FROM performance_schema.data_lock_waits;" || true
      fi
      sleep 0.4
    done
  } > "$OUT/locks-$tag.log" 2>&1 &
  local sampler=$!

  python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" --label "locks-$tag" >/dev/null 2>&1 || true
  kill "$sampler" 2>/dev/null || true
  wait "$sampler" 2>/dev/null || true
  echo "  locks-$tag.log  $(grep -c '^-- sample' "$OUT/locks-$tag.log") samples"
}

log "5b/6  Lock evidence, on its own load"
lock_evidence postgres repeatable-read count_then_insert pg-rr-cti
lock_evidence mysql    repeatable-read count_then_insert my-rr-cti
lock_evidence postgres read-committed  where_guard       pg-rc-guard
lock_evidence mysql    read-committed  where_guard       my-rc-guard

log "6/6  Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
