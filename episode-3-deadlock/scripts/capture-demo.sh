#!/usr/bin/env bash
#
# Runs the Episode 3 matrix and records what actually happened.
#
#   ./scripts/capture-demo.sh
#
# The episode's claim is that a handler with no defect in any line deadlocks
# because of an order nobody chose, and that one word fixes it. So every cell
# runs the SAME handler and the same load; only LOCK_ORDER moves.
#
# Both engines run every cell, because the second measured number is how long
# each one takes to NOTICE the cycle: Postgres runs its detector only after a
# backend has waited deadlock_timeout, InnoDB checks on every lock wait.
#
# Writes:  capture/*.log  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

RESUME=${RESUME:-0}

ORDERS=${ORDERS:-300}
CONCURRENCY=${CONCURRENCY:-25}
STOCK=${STOCK:-100}
BASKET=${BASKET:-3}

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

# One cell: engine x lock order x scenario x retries.
#
# The config is asserted rather than assumed. Attributing a sorted run's zero to
# the basket order because a POST silently failed would be a very quiet way to
# publish a wrong episode.
cell() {
  local engine=$1 order=$2 scenario=$3 retries=$4 items=$5 lockto=$6 tag=$7
  local got

  if [ "$RESUME" = "1" ] && grep -q "^STATS $tag " "$OUT/cell-$tag.log" 2>/dev/null; then
    log "$tag  -- already measured, skipping"
    return 0
  fi

  got=$(curl -fsS -X POST localhost:8000/admin/config -H 'content-type: application/json' \
        -d "{\"engine\":\"$engine\",\"lock_order\":\"$order\",\"scenario\":\"$scenario\",\"retries\":$retries,\"lock_timeout_ms\":$lockto}" \
        | python3 -c 'import json,sys
d = json.load(sys.stdin)
print(d["engine"] + "/" + d["lock_order"] + "/" + d["scenario"] + "/" + str(d["retries"]))')
  [ "$got" = "$engine/$order/$scenario/$retries" ] || { echo "config did not take: wanted $engine/$order/$scenario/$retries, got $got"; exit 1; }

  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null

  # Postgres's own lock log, for THIS cell only. Truncating the container log is
  # not possible, so the read position is marked before the load and everything
  # after it is what this cell produced.
  local pg_mark
  pg_mark=$(dc logs postgres 2>/dev/null | wc -l | tr -d ' ')

  {
    echo "CELL $tag engine=$engine lock_order=$order scenario=$scenario retries=$retries items=$items lock_timeout_ms=$lockto"
    python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" \
      --max-basket-items "$items" --label "$tag"
    echo "STATE $tag $(curl -fsS localhost:8000/api/state)"
    echo "STATS $tag $(curl -fsS localhost:8000/admin/stats)"
  } 2>&1 | tee "$OUT/cell-$tag.log"

  dc logs postgres 2>/dev/null | tail -n +"$((pg_mark + 1))" | grep -E "deadlock|still waiting|acquired" \
    > "$OUT/pglog-$tag.log" 2>/dev/null || true
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
  echo "orders=$ORDERS concurrency=$CONCURRENCY stock=$STOCK basket_items=$BASKET"
} >> "$OUT/01-compose-up.log"

log "3/6  The settings this episode measures, read off the engines"
{
  echo "-- postgres: the detector does not run until a backend has waited this long"
  psql -c "SHOW deadlock_timeout;"
  psql -c "SHOW log_lock_waits;"
  echo
  echo "-- mysql: InnoDB checks for a cycle on every lock wait"
  mysql_ -e "SELECT @@innodb_deadlock_detect, @@innodb_lock_wait_timeout, @@innodb_print_all_deadlocks;"
} 2>&1 | tee "$OUT/02-settings.log"

log "4/6  The matrix"
#      engine   order   scenario        retries items lockto tag
#
# The bug, on both engines. Same handler, same load; the only thing that differs
# is which engine is being asked to notice the cycle.
cell postgres basket  basket_checkout  0 "$BASKET"   0 pg-basket
cell mysql    basket  basket_checkout  0 "$BASKET"   0 my-basket
# The one-word fix, on both.
cell postgres sorted  basket_checkout  0 "$BASKET"   0 pg-sorted
cell mysql    sorted  basket_checkout  0 "$BASKET"   0 my-sorted
# Retrying WITHOUT fixing the order. Three attempts with exponential backoff,
# which is what an application that has read the manual actually does.
cell postgres basket  basket_checkout  3 "$BASKET"   0 pg-basket-retry
# Failing fast instead of stalling: give up after 50ms rather than waiting a
# full second for the detector to run and then being chosen as the victim.
cell postgres basket  basket_checkout  0 "$BASKET"  50 pg-basket-locktimeout
# What you would actually ship: the order fixed, and a retry for the residue.
cell postgres sorted  basket_checkout  3 "$BASKET"   0 pg-sorted-retry
# The trap fix: one statement is not the same claim as one lock order.
cell postgres basket  basket_one_stmt  0 "$BASKET"   0 pg-one-stmt
# The same "one statement" claim, written the other common way. A VALUES join
# hands the planner the rows in the order they were written, which is the
# basket's order.
cell postgres basket  basket_values_join 0 "$BASKET" 0 pg-values-join
# The hide-the-bug exercise: one item per order cannot be half of a cycle.
cell postgres basket  basket_checkout  0 1           0 pg-basket-single

log "4b/6  What the planner actually does with a multi-row UPDATE"
{
  echo "-- IN-list form: the order the rows come back in is the planner's choice"
  psql -c "EXPLAIN UPDATE inventory SET stock = stock - 1 WHERE sku_id IN (8,3,4) AND stock >= 1;"
  echo
  echo "-- VALUES-join form: the rows arrive in the order they were written"
  psql -c "EXPLAIN UPDATE inventory SET stock = inventory.stock - v.qty FROM (VALUES (8,1),(3,1),(4,1)) AS v(sku_id, qty) WHERE inventory.sku_id = v.sku_id AND inventory.stock >= v.qty;"
  echo
  echo "-- neither statement contains an ORDER BY, because an UPDATE cannot take one"
} 2>&1 | tee "$OUT/04-plans.log"

log "5/6  What the engines logged about the cycle"
{
  echo "-- postgres, from the basket-order run. Unsummarised."
  head -24 "$OUT/pglog-pg-basket.log" 2>/dev/null || echo "(none)"
  echo
  echo "-- mysql: the latest deadlock, as InnoDB describes it"
  mysql_ -e "SHOW ENGINE INNODB STATUS\\G" 2>/dev/null \
    | sed -n '/LATEST DETECTED DEADLOCK/,/^---/p' | head -40 || true
} 2>&1 | tee "$OUT/03-deadlocks.log"

log "6/6  Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
