"""Reads one agent's slice of the telemetry DB for a time window.

The DSN must be the agent's own role (agent_<node>). There is no WHERE on source
here on purpose: row-level security decides what the agent can see. node.py
refuses a shard with someone else's rows and verify.py isolation tests it live.

Each row gets an "id" agents can cite as evidence: span:<span_id>, log:<id>, metric:<id>.
"""
import psycopg
from psycopg.rows import dict_row

QUERIES = {
    "traces": "SELECT 'span:' || span_id AS id, trace_id, parent_id, ts, name, target,"
              " duration_ms, status, source, attrs FROM traces",
    "logs": "SELECT 'log:' || id AS id, ts, severity, source, trace_id, message, code FROM logs",
    "metrics": "SELECT 'metric:' || id AS id, ts, metric, value, unit, source, attrs FROM metrics",
}


def get_shard(dsn, start, end, limit=5000, timeout=3):
    shard = {}
    with psycopg.connect(dsn, connect_timeout=timeout, row_factory=dict_row) as conn:
        for table, select in QUERIES.items():
            # past the limit we keep the newest rows, so keep fault windows tight
            shard[table] = conn.execute(
                f"{select} WHERE ts >= %s AND ts < %s ORDER BY ts DESC LIMIT %s",
                (start, end, limit)).fetchall()
    return shard


def sources(shard):
    return sorted({r["source"] for rows in shard.values() for r in rows})
