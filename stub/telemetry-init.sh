#!/bin/sh
# STAND-IN for P3's telemetry schema + roles. P3: replace this file with yours,
# keep the table/column names below or tell P1 first (otel.py writes them).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE TABLE traces (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT,
  ts TIMESTAMPTZ NOT NULL, name TEXT, target TEXT, duration_ms DOUBLE PRECISION,
  status TEXT, source TEXT NOT NULL, attrs JSONB
);
CREATE TABLE metrics (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, metric TEXT, value DOUBLE PRECISION,
  unit TEXT, source TEXT NOT NULL, attrs JSONB
);
CREATE TABLE logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, severity TEXT, source TEXT NOT NULL,
  trace_id TEXT, message TEXT, code TEXT
);
CREATE INDEX ON traces (source, ts);
CREATE INDEX ON traces (trace_id);
CREATE INDEX ON metrics (source, ts);
CREATE INDEX ON logs (source, ts);

-- One read-only role per machine; row-level security limits it to its own source.
CREATE ROLE agents NOLOGIN;
GRANT SELECT ON traces, metrics, logs TO agents;
ALTER TABLE traces  ENABLE ROW LEVEL SECURITY;
ALTER TABLE metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE logs    ENABLE ROW LEVEL SECURITY;
SQL
for src in $AGENT_SOURCES; do
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<SQL
CREATE ROLE agent_$src LOGIN PASSWORD '$AGENT_DB_PASSWORD' IN ROLE agents;
CREATE POLICY own_$src ON traces  FOR SELECT TO agent_$src USING (source = '$src');
CREATE POLICY own_$src ON metrics FOR SELECT TO agent_$src USING (source = '$src');
CREATE POLICY own_$src ON logs    FOR SELECT TO agent_$src USING (source = '$src');
SQL
done
