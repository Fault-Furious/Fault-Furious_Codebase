#!/bin/sh
# Telemetry DB schema and roles (P3). Replaces stub/telemetry-init.sh.
#
# Table and column names are the agreed interface: otel.py writes them, agent/shard.py
# reads them. Everything here only adds to that shape, so neither has to change.
#
# Isolation: each agent_<source> role sees only rows whose source is its own machine.
# Row-level security enforces it, for the tables and for every view below (the views
# are security_invoker, so they run with the agent's rights, not the owner's).
set -e

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v db="$POSTGRES_DB" \
  -v writer_pw="${TELEMETRY_WRITER_PASSWORD:-writer}" <<'SQL'
-- The machines that produce telemetry. Same IP for the same server, always.
CREATE TABLE machines (
  source   TEXT PRIMARY KEY,
  location TEXT,
  ip       INET,
  role     TEXT,  -- 'app' or 'db'
  stack    TEXT
);

CREATE TABLE traces (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT,
  ts TIMESTAMPTZ NOT NULL, name TEXT, target TEXT, duration_ms DOUBLE PRECISION,
  status TEXT, source TEXT NOT NULL REFERENCES machines, attrs JSONB
);
CREATE TABLE metrics (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, metric TEXT, value DOUBLE PRECISION,
  unit TEXT, source TEXT NOT NULL REFERENCES machines, attrs JSONB,
  -- otel.py puts the trace id in attrs; this exposes it as a column without changing otel.py
  trace_id TEXT GENERATED ALWAYS AS (attrs->>'trace_id') STORED
);
CREATE TABLE logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, severity TEXT,
  source TEXT NOT NULL REFERENCES machines, trace_id TEXT, message TEXT, code TEXT
);
-- No CHECKs on severity/status: otel.py retries a failed batch forever, so one odd
-- value would stall all of a server's telemetry. An unknown source fails loudly instead.

CREATE INDEX ON traces (source, ts);
CREATE INDEX ON traces (trace_id);
CREATE INDEX ON traces (parent_id);
CREATE INDEX ON metrics (source, ts);
CREATE INDEX ON metrics (source, metric, ts);
CREATE INDEX ON metrics (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX ON logs (source, ts);
CREATE INDEX ON logs (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX ON logs (code, ts) WHERE code IS NOT NULL;

-- Coarse device class from a user agent, for the "is it mobile or desktop" question.
CREATE FUNCTION device_type(user_agent TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
    WHEN coalesce(user_agent, '') = '' THEN 'unknown'
    WHEN user_agent ~* 'bot|crawl|spider|curl|python|wget' THEN 'bot'
    WHEN user_agent ~* 'ipad|tablet' THEN 'tablet'
    WHEN user_agent ~* 'iphone|android|mobile' THEN 'mobile'
    ELSE 'desktop'
  END
$$;

-- One row per HTTP request handled by a server (root spans, plus the cross-server
-- /api/online calls whose parent lives on the other machine).
CREATE VIEW http_requests WITH (security_invoker = true) AS
SELECT span_id, trace_id, parent_id, ts, source, duration_ms, status,
       split_part(name, ' ', 1) AS method,
       split_part(name, ' ', 2) AS path,
       CASE
         WHEN split_part(name, ' ', 2) IN ('/', '/login', '/signup') THEN split_part(name, ' ', 2)
         WHEN split_part(name, ' ', 2) LIKE '/api/%' THEN split_part(name, ' ', 2)
         ELSE '/{forum}'
       END AS route,
       (attrs->>'status_code')::int AS status_code,
       attrs->>'device' AS user_agent,
       device_type(attrs->>'device') AS device_type,
       attrs->>'username' AS username,
       attrs->>'forum' AS forum
FROM traces
WHERE target = 'http';

-- Page views per minute: GETs of real pages by visitors. Not API calls, and not
-- bots (Docker's healthcheck polls /login every 3 s with a Python user agent).
CREATE VIEW page_views WITH (security_invoker = true) AS
SELECT date_trunc('minute', ts) AS minute, source, route, device_type,
       count(*) AS views,
       count(*) FILTER (WHERE status_code >= 500) AS server_errors,
       round(avg(duration_ms)::numeric, 1) AS avg_ms
FROM http_requests
WHERE method = 'GET' AND route NOT LIKE '/api/%' AND device_type <> 'bot'
GROUP BY 1, 2, 3, 4;

-- New visitors = signups, returning visitors = successful logins, per minute.
CREATE VIEW visitors WITH (security_invoker = true) AS
SELECT date_trunc('minute', ts) AS minute, source,
       coalesce(sum(value) FILTER (WHERE metric = 'signups'), 0) AS new_visitors,
       coalesce(sum(value) FILTER (WHERE metric = 'login.attempts' AND attrs->>'ok' = 'true'), 0) AS returning_visitors,
       coalesce(sum(value) FILTER (WHERE metric = 'login.attempts' AND attrs->>'ok' = 'false'), 0) AS failed_logins
FROM metrics
WHERE metric IN ('signups', 'login.attempts')
GROUP BY 1, 2;

-- Warnings and errors per minute, by code (53300, 23505, PEER_UNREACHABLE, ...).
CREATE VIEW log_codes WITH (security_invoker = true) AS
SELECT date_trunc('minute', ts) AS minute, source, severity, code, count(*) AS n
FROM logs
WHERE severity NOT IN ('DEBUG', 'INFO')
GROUP BY 1, 2, 3, 4;

-- Clean-up: traffic adds roughly 30 rows a second. Call as the owner, e.g.
--   SELECT * FROM telemetry_prune('6 hours');
CREATE FUNCTION telemetry_prune(keep INTERVAL)
RETURNS TABLE (traces BIGINT, metrics BIGINT, logs BIGINT)
LANGUAGE plpgsql AS $$
DECLARE cutoff TIMESTAMPTZ := now() - keep;
BEGIN
  DELETE FROM public.traces t WHERE t.ts < cutoff;   GET DIAGNOSTICS traces = ROW_COUNT;
  DELETE FROM public.metrics m WHERE m.ts < cutoff;  GET DIAGNOSTICS metrics = ROW_COUNT;
  DELETE FROM public.logs l WHERE l.ts < cutoff;     GET DIAGNOSTICS logs = ROW_COUNT;
  RETURN NEXT;
END
$$;
REVOKE EXECUTE ON FUNCTION telemetry_prune(INTERVAL) FROM PUBLIC;

-- Nobody gets in by default; roles below are granted exactly what they need.
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

ALTER TABLE machines ENABLE ROW LEVEL SECURITY;
ALTER TABLE traces   ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics  ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs     ENABLE ROW LEVEL SECURITY;

-- Agents: read-only; a per-source policy (below) decides which rows they see.
CREATE ROLE agents NOLOGIN;
GRANT CONNECT ON DATABASE :"db" TO agents;
GRANT USAGE ON SCHEMA public TO agents;
GRANT SELECT ON machines, traces, metrics, logs, http_requests, page_views, visitors, log_codes TO agents;
GRANT EXECUTE ON FUNCTION device_type(TEXT) TO agents;

-- Writer for otel.py: insert only, cannot read anything back. Not used yet: the
-- services still connect as the owner until P1 switches TELEMETRY_DSN in compose.
CREATE ROLE telemetry_writer LOGIN PASSWORD :'writer_pw';
GRANT CONNECT ON DATABASE :"db" TO telemetry_writer;
GRANT USAGE ON SCHEMA public TO telemetry_writer;
GRANT INSERT ON traces, metrics, logs TO telemetry_writer;
GRANT USAGE ON SEQUENCE metrics_id_seq, logs_id_seq TO telemetry_writer;
CREATE POLICY writer_insert ON traces  FOR INSERT TO telemetry_writer WITH CHECK (true);
CREATE POLICY writer_insert ON metrics FOR INSERT TO telemetry_writer WITH CHECK (true);
CREATE POLICY writer_insert ON logs    FOR INSERT TO telemetry_writer WITH CHECK (true);
SQL

# One machine row and one agent role per source.
for src in $AGENT_SOURCES; do
  case $src in
    stockholm) loc=Stockholm ip=${STOCKHOLM_IP:-170.57.176.88}   role=app stack="Python, Ubuntu" ;;
    helsinki)  loc=Helsinki  ip=${HELSINKI_IP:-104.172.209.255}  role=app stack="Python, Ubuntu" ;;
    vantaa)    loc=Vantaa    ip=${VANTAA_IP:-88.211.152.184}     role=db  stack="PostgreSQL 16, Ubuntu" ;;
    *)         loc= ip= role= stack= ;;
  esac
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v src="$src" -v agent="agent_$src" -v policy="own_$src" -v pw="$AGENT_DB_PASSWORD" \
    -v loc="$loc" -v ip="$ip" -v role="$role" -v stack="$stack" <<'SQL'
INSERT INTO machines VALUES (:'src', nullif(:'loc', ''), nullif(:'ip', '')::inet,
                             nullif(:'role', ''), nullif(:'stack', ''));
CREATE ROLE :"agent" LOGIN PASSWORD :'pw' IN ROLE agents;
CREATE POLICY :"policy" ON machines FOR SELECT TO :"agent" USING (source = :'src');
CREATE POLICY :"policy" ON traces   FOR SELECT TO :"agent" USING (source = :'src');
CREATE POLICY :"policy" ON metrics  FOR SELECT TO :"agent" USING (source = :'src');
CREATE POLICY :"policy" ON logs     FOR SELECT TO :"agent" USING (source = :'src');
SQL
done
