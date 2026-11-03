"""Telemetry sidecar for the user database (Vantaa). Postgres can't import otel.py,
so this tails Postgres's own CSV log and polls pg_stat_activity, emitting through otel.

Refused connections (SQLSTATE 53300) never reach a SQL session, so they only exist
in the server log. That is why this tails the log instead of querying.
"""
import csv
import glob
import os
import time

import psycopg

import otel

DSN = os.environ["USER_DB_DSN"]
LOG_DIR = os.environ.get("PG_LOG_DIR", "/pglog")
POLL = float(os.environ.get("DBMON_POLL_SECONDS", "1"))
SEVERITY = {"PANIC": "FATAL", "FATAL": "ERROR", "ERROR": "ERROR", "WARNING": "WARN", "LOG": "INFO"}

# csvlog columns (PG16): 11 = error_severity, 12 = sql_state_code, 13 = message
SEV, CODE, MSG = 11, 12, 13


def follow(path, pos):
    """Return new complete CSV records after byte offset pos, and the new offset."""
    with open(path, newline="") as f:
        f.seek(pos)
        data = f.read()
    end = data.rfind("\n") + 1  # only consume complete lines; a record may still be mid-write
    # ponytail: assumes no newlines inside quoted fields at the cut point; fine for connection errors
    return list(csv.reader(data[:end].splitlines())), pos + len(data[:end].encode())


def poll_activity(conn):
    total, active, maxc = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE state = 'active'),"
        " current_setting('max_connections')::int FROM pg_stat_activity WHERE backend_type = 'client backend'"
    ).fetchone()
    otel.metric("db.connections", total, "count")
    otel.metric("db.connections.active", active, "count")
    otel.metric("db.connections.max", maxc, "count")


def main():
    path, pos, conn = None, 0, None
    while True:
        time.sleep(POLL)
        files = sorted(glob.glob(f"{LOG_DIR}/*.csv"))
        if files and files[-1] != path:
            path, pos = files[-1], 0
        if path:
            records, pos = follow(path, pos)
            refused = 0
            for r in records:
                if len(r) <= MSG or r[SEV] not in SEVERITY or r[SEV] == "LOG":
                    continue
                refused += r[CODE] == "53300"
                otel.log(SEVERITY[r[SEV]], r[MSG], code=r[CODE] or None)
            otel.metric("db.connections.refused", refused, "count")
        try:
            # superuser_reserved_connections keeps a slot for us even when clients are refused
            conn = conn if conn and not conn.closed else psycopg.connect(DSN, autocommit=True)
            poll_activity(conn)
        except psycopg.Error as e:
            otel.log("ERROR", f"dbmon cannot query activity: {e}", code=getattr(e, "sqlstate", None))
            conn = None


if __name__ == "__main__":
    main()
