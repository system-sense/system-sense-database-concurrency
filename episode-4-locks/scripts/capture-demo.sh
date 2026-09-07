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

  # DETAIL and CONTEXT are the point, and the first version of this grep dropped
  # both: a DETAIL line reads "Process 1234 waits for ShareLock on transaction
  # 5678; blocked by process 1235" and contains none of the words that were
  # being matched. The cycle itself was being filtered out of the evidence.
  dc logs postgres 2>/dev/null | tail -n +"$((pg_mark + 1))" \
    | grep -E "deadlock|still waiting|acquired|DETAIL|CONTEXT|HINT" \
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

# Postgres's own deadlock report, on its own load.
#
# A SEPARATE pass, and deliberately so. The measured cells above are what the
# voiceover quotes, and re-running one to improve its logging would change the
# numbers the narration was written against -- which PRODUCTION.md forbids for
# exactly this reason. So this fires its own load and captures the whole report,
# and produces NO figure the episode quotes.
deadlock_evidence() {
  curl -fsS -X POST localhost:8000/admin/config -H 'content-type: application/json' \
    -d '{"engine":"postgres","lock_order":"basket","scenario":"basket_checkout","retries":0,"lock_timeout_ms":0}' >/dev/null
  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null
  local mark
  mark=$(dc logs postgres 2>/dev/null | wc -l | tr -d ' ')
  python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" \
    --max-basket-items "$BASKET" --label evidence >/dev/null 2>&1 || true
  # Anchored on "deadlock detected" and the lines that follow it. A bare DETAIL
  # grep does not work here: log_min_duration_statement=0 makes Postgres emit a
  # DETAIL line carrying the bound parameters for EVERY statement, and forty of
  # those arrive before the first deadlock report does.
  {
    echo "-- evidence only: the counts from this load are NOT the episode's numbers"
    dc logs postgres 2>/dev/null | tail -n +"$((mark + 1))" \
      | sed 's/^postgres-1  | //' \
      | grep -A5 "deadlock detected" | head -30
  } > "$OUT/05-deadlock-report.log" 2>&1
  echo "  05-deadlock-report.log  $(grep -c 'deadlock detected' "$OUT/05-deadlock-report.log" || echo 0) reports"
}

log "4c/6  Postgres's deadlock report, on its own load"
deadlock_evidence

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
