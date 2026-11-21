#!/bin/sh
# P3 checks against the running stack (docker compose up -d first, give it ~10 s of traffic).
# Every agent role sees only its own machine, the writer can't read, agents can't write,
# the views respect isolation, and metrics.trace_id links to traces. Last line PASS or FAIL.
cd "$(dirname "$0")/.."
sources=${AGENT_SOURCES:-stockholm helsinki vantaa}
pw=${AGENT_DB_PASSWORD:-agent}
fail=0

# psql inside the telemetry container, as the given role
q() {
  role=$1 pass=$2; shift 2
  docker compose exec -T -e PGPASSWORD="$pass" telemetrydb \
    psql -h localhost -U "$role" -d "${TELEMETRY_DB:-telemetry}" -v ON_ERROR_STOP=1 -At "$@"
}
ok()  { echo "ok   $1"; }
bad() { echo "FAIL $1"; fail=1; }

admin=${TELEMETRY_USER:-telemetry}
apw=${TELEMETRY_PASSWORD:-telemetry}
# init scripts only run on an empty volume, so an old stub schema can still be in place
if [ "$(q $admin $apw -c "SELECT to_regclass('public.machines') IS NOT NULL")" != t ]; then
  echo "FAIL telemetry DB was not built from telemetry/init.sh"
  echo "     point telemetrydb at ./telemetry/init.sh in docker-compose.yml, then: docker compose down -v && docker compose up -d --build"
  echo FAIL
  exit 1
fi

for src in $sources; do
  r=agent_$src
  seen=$(q $r $pw -c "SELECT string_agg(DISTINCT source, ',') FROM (
           SELECT source FROM traces UNION ALL SELECT source FROM metrics UNION ALL
           SELECT source FROM logs UNION ALL SELECT source FROM machines) s")
  [ "$seen" = "$src" ] && ok "$r sees only $src" || bad "$r sees '$seen'"
  if seen=$(q $r $pw -c "SELECT coalesce(string_agg(DISTINCT source, ','), '') FROM (
           SELECT source FROM http_requests UNION ALL SELECT source FROM page_views UNION ALL
           SELECT source FROM visitors UNION ALL SELECT source FROM log_codes) s"); then
    case $seen in ''|"$src") ok "$r views stay on $src" ;; *) bad "$r views show '$seen'" ;; esac
  else
    bad "$r cannot read the views"
  fi
  q $r $pw -c "INSERT INTO logs (ts, source, message) VALUES (now(), '$src', 'x')" >/dev/null 2>&1 \
    && bad "$r can insert" || ok "$r cannot insert"
  q $r $pw -c "SELECT telemetry_prune('1 day')" >/dev/null 2>&1 \
    && bad "$r can prune" || ok "$r cannot prune"
done

wpw=${TELEMETRY_WRITER_PASSWORD:-writer}
q telemetry_writer $wpw -c "INSERT INTO logs (ts, severity, source, message) VALUES (now(), 'DEBUG', 'vantaa', 'p3 check')" >/dev/null \
  && ok "writer can insert" || bad "writer cannot insert"
# these must fail for the right reason, not because a role or table is missing
q telemetry_writer $wpw -c "SELECT count(*) FROM logs" 2>&1 | grep -q "permission denied" \
  && ok "writer cannot read" || bad "writer can read (or failed for another reason)"
q telemetry_writer $wpw -c "INSERT INTO logs (ts, source, message) VALUES (now(), 'uppsala', 'x')" 2>&1 | grep -q "foreign key" \
  && ok "unknown source rejected" || bad "unknown source accepted (or failed for another reason)"

n=$(q $admin $apw -c "SELECT count(*) FROM metrics m JOIN traces t USING (trace_id)")
[ "${n:-0}" -gt 0 ] && ok "metrics.trace_id joins traces ($n rows)" || bad "no metric joins a trace (no traffic yet?)"

[ $fail = 0 ] && echo PASS || echo FAIL
