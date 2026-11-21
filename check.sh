#!/bin/sh
# Smoke test against the running stack: one /login must land a parent span, a child
# span, a log line and a metric in the telemetry DB, all with the same trace id.
set -e
cd "$(dirname "$0")"
url=http://localhost:${STOCKHOLM_PORT:-8001}
user=check$(date +%s)
curl -sf -o /dev/null -d username=$user -d email=c@x.fi -d password=pw $url/signup -w '' || true
curl -s -o /dev/null -d username=$user -d password=pw $url/login
sleep 3
docker compose exec -T telemetrydb psql -U ${TELEMETRY_USER:-telemetry} -d ${TELEMETRY_DB:-telemetry} -v ON_ERROR_STOP=1 -At <<SQL
SELECT 'trace ' || t.trace_id
     || ' spans=' || (SELECT count(*) FROM traces WHERE trace_id = t.trace_id)
     || ' logs='  || (SELECT count(*) FROM logs WHERE trace_id = t.trace_id)
     || ' metrics=' || (SELECT count(*) FROM metrics WHERE attrs->>'trace_id' = t.trace_id)
FROM traces t JOIN traces c ON c.parent_id = t.span_id
WHERE t.name = 'POST /login' AND t.attrs->>'username' = '$user';
SELECT CASE WHEN EXISTS (
  SELECT 1 FROM traces t
  JOIN traces c  ON c.parent_id = t.span_id
  JOIN logs l    ON l.trace_id = t.trace_id
  JOIN metrics m ON m.attrs->>'trace_id' = t.trace_id
  WHERE t.name = 'POST /login' AND t.parent_id IS NULL AND t.attrs->>'username' = '$user'
) THEN 'PASS' ELSE 'FAIL' END;
SQL
