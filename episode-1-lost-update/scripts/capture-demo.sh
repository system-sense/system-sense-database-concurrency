#!/usr/bin/env bash
#
# Runs the Episode 1 demo end to end and records what actually happened.
#
# Everything the episode claims on screen comes out of this script. If a number
# changes when you run it on your machine, the number was real.
#
#   ./scripts/capture-demo.sh
#
# Writes:  capture/*.log  and  capture/metrics.json
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=capture
mkdir -p "$OUT"

ORDERS=${ORDERS:-300}
CONCURRENCY=${CONCURRENCY:-25}
STOCK=${STOCK:-100}

log()   { printf '\n\033[1m== %s\033[0m\n' "$*"; }
dc()    { docker compose "$@"; }
psql()  { dc exec -T postgres psql -U sysense -d sysense -At -F' ' "$@"; }
psqlt() { dc exec -T postgres psql -U sysense -d sysense "$@"; }

wait_healthy() {
  printf 'waiting for the stack '
  for _ in $(seq 1 90); do
    if curl -fsS localhost:8000/health >/dev/null 2>&1 &&
       curl -fsS localhost:9000/health >/dev/null 2>&1; then echo ' ready'; return 0; fi
    printf '.'; sleep 1
  done
  echo ' TIMED OUT'; dc logs app pricing | tail -40; return 1
}

# Reads the shelf and the order book and prints one machine-parsable line.
tally() {
  local name=$1 stock orders units
  stock=$(psql -c 'SELECT stock FROM inventory WHERE sku_id = 1;')
  orders=$(psql -c 'SELECT count(*) FROM orders;')
  units=$(psql -c 'SELECT coalesce(sum(qty),0) FROM orders;')
  echo "RESULT $name stock_before=$STOCK stock_after=$stock orders_created=$orders units_sold=$units"
}

# One scenario: set the mode, put the shelf back, fire the orders, read both books.
#
# The mode is asserted rather than assumed. Running the naive numbers against
# the atomic handler because a POST silently failed would be a very quiet way to
# publish a wrong episode.
run_mode() {
  local mode=$1 n=$2
  log "$n  ORDER_MODE=$mode"
  local got
  got=$(curl -fsS -X POST localhost:8000/admin/mode \
          -H 'content-type: application/json' -d "{\"mode\":\"$mode\"}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')
  [ "$got" = "$mode" ] || { echo "mode did not take: wanted $mode, got $got"; exit 1; }
  curl -fsS -X POST "localhost:8000/admin/reset?sku_stock=$STOCK" >/dev/null

  {
    python3 scripts/order.py --orders "$ORDERS" --concurrency "$CONCURRENCY" --label "$mode"
    tally "$mode"
  } 2>&1 | tee "$OUT/$n-$mode.log"

  curl -fsS localhost:8000/admin/stats > "$OUT/stats-$mode.json"
  echo "  app counters -> $OUT/stats-$mode.json"
}

log "1/8  Tearing down any previous run"
dc down -v --remove-orphans >/dev/null 2>&1 || true

log "2/8  Starting the stack"
dc up --build -d 2>&1 | tee "$OUT/01-compose-up.log"
wait_healthy
{
  echo "pricing latency = PRICING_BASE_MS + (sku_id * 137 + customer_id * 31) % PRICING_SPREAD_MS"
  dc exec -T pricing printenv PRICING_BASE_MS PRICING_SPREAD_MS | tr '\n' ' '
  echo
  printf 'max optimistic retries = '
  dc exec -T app printenv MAX_OPTIMISTIC_RETRIES | tr -d '\r'
  echo
  echo "orders=$ORDERS concurrency=$CONCURRENCY stock=$STOCK"
} >> "$OUT/01-compose-up.log"

run_mode naive       02
run_mode atomic      03
run_mode pessimistic 04
run_mode optimistic  05

log "6/8  The shelf and the order book, side by side"
{
  echo "-- after the naive run the numbers are in capture/02-naive.log;"
  echo "-- this is the state the LAST run left behind, for orientation only."
  psqlt -c "SELECT sku_id, name, stock, version FROM inventory;"
  psqlt -c "SELECT count(*) AS orders, coalesce(sum(qty),0) AS units_sold FROM orders;"
} 2>&1 | tee "$OUT/06-books.log"

log "7/8  The constraint that never fired"
{
  echo "-- the guard every reviewer asks for"
  psqlt -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'stock_never_negative';"
  echo
  echo "-- times it fired during the naive run, from the application's own counter:"
  python3 -c "import json;print(json.load(open('capture/stats-naive.json'))['check_constraint_violations'])"
} 2>&1 | tee "$OUT/07-constraint.log"

log "8/8  Summarising"
python3 scripts/summarise.py

echo
echo "Done. Real numbers are in $OUT/metrics.json"
