"""Telemetry module. The only way any service writes telemetry rows.

    with start_span("GET /login", target="app") as span: ...
    log("ERROR", "db refused", code="53300")
    metric("http.requests", 1, "count")

Rows are queued and flushed by a background thread about once a second, so
telemetry latency never becomes request latency.
"""
import atexit
import contextvars
import json
import os
import queue
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg import sql

SOURCE = os.environ["OTEL_SOURCE"]          # machine name, e.g. "stockholm"
DSN = os.environ["TELEMETRY_DSN"]
FLUSH_SECONDS = float(os.environ.get("OTEL_FLUSH_SECONDS", "1"))
TRACES = os.environ.get("OTEL_TRACES_TABLE", "traces")
METRICS = os.environ.get("OTEL_METRICS_TABLE", "metrics")
LOGS = os.environ.get("OTEL_LOGS_TABLE", "logs")

# (trace_id, span_id) of the span currently open in this request/task
_current = contextvars.ContextVar("otel_current", default=(None, None))
_q = queue.Queue(maxsize=100_000)


def _now():
    return datetime.now(timezone.utc)


def _put(table, row):
    try:
        _q.put_nowait((table, row))
    except queue.Full:  # ponytail: drops on overload, add a dropped-rows counter if it ever happens
        pass


_span_attrs = contextvars.ContextVar("otel_span_attrs", default=None)


def annotate(**attrs):
    """Add attrs (username, forum id, ...) to the current span."""
    d = _span_attrs.get()
    if d is not None:
        d.update(attrs)


def current_trace():
    return _current.get()


def traceparent():
    """W3C traceparent header value for the current span, for outgoing calls."""
    trace_id, span_id = _current.get()
    return f"00-{trace_id}-{span_id}-01" if trace_id else None


def parse_traceparent(value):
    """(trace_id, span_id) from an incoming traceparent header, or None."""
    parts = (value or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and len(parts[2]) == 16:
        return parts[1], parts[2]
    return None


class Span:
    def __init__(self, attrs):
        self.attrs = attrs
        self.status = "OK"


@contextmanager
def start_span(name, target, parent=None, **attrs):
    """parent: (trace_id, span_id) to continue a remote trace; default is the current span."""
    trace_id, parent_id = parent or _current.get()
    trace_id = trace_id or secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    token = _current.set((trace_id, span_id))
    span = Span(attrs)
    attrs_token = _span_attrs.set(span.attrs)
    ts, t0 = _now(), time.perf_counter()
    try:
        yield span
    except BaseException as e:
        span.status = "ERROR"
        span.attrs.setdefault("error", type(e).__name__)
        raise
    finally:
        _current.reset(token)
        _span_attrs.reset(attrs_token)
        _put(TRACES, dict(
            span_id=span_id, trace_id=trace_id, parent_id=parent_id, ts=ts,
            name=name, target=target,
            duration_ms=(time.perf_counter() - t0) * 1000,
            status=span.status, source=SOURCE, attrs=json.dumps(span.attrs, default=str),
        ))


def log(severity, message, code=None):
    _put(LOGS, dict(ts=_now(), severity=severity, source=SOURCE,
                    trace_id=_current.get()[0], message=message, code=code))


def metric(name, value, unit, **attrs):
    trace_id = _current.get()[0]
    if trace_id:  # metrics table has no trace_id column; attrs carries the link
        attrs["trace_id"] = trace_id
    _put(METRICS, dict(ts=_now(), metric=name, value=value, unit=unit,
                       source=SOURCE, attrs=json.dumps(attrs, default=str)))


def _drain():
    rows = []
    while True:
        try:
            rows.append(_q.get_nowait())
        except queue.Empty:
            return rows


def _write(conn, rows):
    by_table = {}
    for table, row in rows:
        by_table.setdefault(table, []).append(row)
    with conn.cursor() as cur:
        for table, batch in by_table.items():
            cols = list(batch[0])
            stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder() * len(cols)),
            )
            cur.executemany(stmt, [[r[c] for c in cols] for r in batch])
    conn.commit()


def _flusher():
    conn = None
    pending = []
    while True:
        time.sleep(FLUSH_SECONDS)
        pending += _drain()
        if not pending:
            continue
        try:
            conn = conn if conn and not conn.closed else psycopg.connect(DSN)
            _write(conn, pending)
            pending = []
        except Exception as e:
            # keep rows and retry next tick; cap so a dead telemetry DB can't eat memory
            print(f"otel: flush failed ({e!r}), {len(pending)} rows pending", file=sys.stderr)
            pending = pending[-100_000:]
            if conn:
                conn.close()
            conn = None


def flush():
    """Write everything queued right now (used at shutdown)."""
    rows = _drain()
    if rows:
        with psycopg.connect(DSN) as conn:
            _write(conn, rows)


threading.Thread(target=_flusher, daemon=True, name="otel-flusher").start()
atexit.register(lambda: flush() if not _q.empty() else None)
