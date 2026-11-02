#!/bin/sh
# Three agent nodes on this machine, no Docker. Ports 9001-9003.
#   agent/run_local.sh        start, mock only, no DB
#   agent/run_local.sh db     start, each node reads localhost:5433 with its own role
#   agent/run_local.sh stop
set -e
cd "$(dirname "$0")/.."
tmp=${TMPDIR:-/tmp}
run=${AGENT_RUN_DIR:-${tmp%/}/ff-agents}
py=${PYTHON:-.venv/bin/python}
[ -x "$py" ] || py=python3
nodes="stockholm:9001 helsinki:9002 vantaa:9003"

stop() {
  for f in "$run"/*.pid; do
    [ -f "$f" ] || continue
    kill "$(cat "$f")" 2>/dev/null || true  # already gone is fine
    rm -f "$f"
  done
}

if [ "$1" = stop ]; then
  stop
  exit 0
fi

for n in $nodes; do
  if curl -s -o /dev/null "http://localhost:${n#*:}/health"; then
    echo "port ${n#*:} is taken, run agent/run_local.sh stop or stop the docker agents first"
    exit 1
  fi
done

mkdir -p "$run"
for n in $nodes; do
  name=${n%:*} port=${n#*:} peers= dsn=
  for p in $nodes; do
    [ "${p%:*}" = "$name" ] || peers="$peers${peers:+,}${p%:*}=http://localhost:${p#*:}"
  done
  if [ "$1" = db ]; then
    dsn="postgresql://agent_$name:${AGENT_DB_PASSWORD:-agent}@localhost:${TELEMETRY_PORT:-5433}/${TELEMETRY_DB:-telemetry}"
  fi
  NODE_ID=$name PEERS=$peers TELEMETRY_DSN=$dsn PYTHONPATH=.:agent \
    "$py" -m uvicorn node:app --port "$port" >"$run/$name.log" 2>&1 &
  echo $! >"$run/$name.pid"
done

# wait until each one answers, a node that died on start (port, import error) fails here
for n in $nodes; do
  name=${n%:*} port=${n#*:} tries=0
  until curl -s -o /dev/null "http://localhost:$port/health"; do
    if ! kill -0 "$(cat "$run/$name.pid")" 2>/dev/null || [ $((tries += 1)) -gt 40 ]; then
      echo "$name did not start:"
      tail -5 "$run/$name.log"
      stop
      exit 1
    fi
    sleep 0.25
  done
  echo "$name on http://localhost:$port, log in $run/$name.log"
done
