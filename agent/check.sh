#!/bin/sh
# Agent checks, last line is PASS or FAIL.
#   agent/check.sh          pytest + three nodes on this machine (no Docker)
#   agent/check.sh stack    whole platform + agents in Docker: isolation, gossip, restart
#   agent/check.sh faults   real surge and split, agents must find them and cite real rows
#   agent/check.sh all      everything above, then stops the stack
set -e
cd "$(dirname "$0")/.."
py=${PYTHON:-.venv/bin/python}
[ -x "$py" ] || py=python3
compose="docker compose"
urls="http://localhost:9001 http://localhost:9002 http://localhost:9003"
verify() { "$py" agent/verify.py "$@"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

trap 'echo FAIL' EXIT

local_nodes() {
  "$py" -m pytest -q
  agent/run_local.sh stop
  agent/run_local.sh
  trap 'agent/run_local.sh stop; echo FAIL' EXIT
  verify health $urls
  for s in surge split none; do verify converge $s $urls; done
  agent/run_local.sh stop
  trap 'echo FAIL' EXIT
  verify down $urls
}

stack() {
  agent/run_local.sh stop  # they would hold the same ports
  $compose up -d --build --wait
  [ "$(./check.sh | tail -1)" = PASS ] || { echo "platform ./check.sh failed"; exit 1; }
  echo "ok   platform ./check.sh PASS"
  verify health $urls
  verify isolation $urls
  # straight to the DB too: helsinki's role must not see stockholm's rows
  q="SELECT (SELECT count(*) FROM traces WHERE source = 'stockholm') || ' ' || (SELECT count(*) FROM traces)"
  set -- $($compose exec -T telemetrydb psql -At \
    "postgresql://agent_helsinki:${AGENT_DB_PASSWORD:-agent}@localhost/${TELEMETRY_DB:-telemetry}" -c "$q")
  [ "$1" = 0 ] && [ "$2" -gt 0 ] || { echo "agent_helsinki sees $1 stockholm rows of $2"; exit 1; }
  echo "ok   agent_helsinki sees 0 stockholm rows, $2 rows of its own"
  verify converge surge $urls
  # a node that comes back mid-incident has to rejoin from gossip alone
  $compose restart agent-vantaa
  echo "..   agent-vantaa restarted"
  verify same $urls
}

fault() {  # fault script, scenario
  start=$(now)
  "$1"
  echo "..   $1 on, ${FAULT_SECONDS:-30}s"
  sleep "${FAULT_SECONDS:-30}"
  "$1" reset
  sleep 5  # let otel and dbmon flush
  # no scenario hint: the mock has to find the fault in its own rows
  verify converge "$2" --detect --real --start "$start" --end "$(now)" $urls
}

faults() {
  $compose ps --status running -q agent-vantaa | grep -q . || stack
  fault faults/signup_surge.sh surge
  fault faults/split.sh split
  # the split must not leave anything wrong on the database side (23505 = username taken)
  errors=$($compose exec -T telemetrydb psql -At \
    "postgresql://agent_vantaa:${AGENT_DB_PASSWORD:-agent}@localhost/${TELEMETRY_DB:-telemetry}" \
    -c "SELECT count(*) FROM logs WHERE ts >= '$start' AND severity = 'ERROR' AND code IS DISTINCT FROM '23505'")
  [ "$errors" = 0 ] || { echo "vantaa has $errors errors during the split"; exit 1; }
  echo "ok   vantaa has no errors during the split"
  # quiet traffic is the control case: only vantaa saying the db is fine
  start=$(now)
  sleep 15
  verify converge none --detect --real --start "$start" --end "$(now)" $urls
}

case "${1:-local}" in
  local) local_nodes ;;
  stack) stack ;;
  faults) faults ;;
  all) local_nodes; stack; faults; $compose down -v ;;
  *) echo "usage: agent/check.sh [local|stack|faults|all]"; exit 2 ;;
esac
trap - EXIT
echo PASS
