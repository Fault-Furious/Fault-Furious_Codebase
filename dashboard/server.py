"""Live dashboard for the three agents and the telemetry they read.

    GET  /              the page
    GET  /api/status    every agent's /health and /state, plus 5 min of telemetry per machine
    POST /api/incident  start one incident on all agents (window = last 60 s)
    POST /api/fault/{surge|split}/{on|off}  run the fault script, needs REPO_DIR and the Docker socket

It reads telemetry the way the agents do: one connection per machine, each with that
machine's own agent role. It never uses the telemetry owner.

Run: uvicorn server:app --port 9000  (agent/mock.py must be importable)
"""
import json
import os
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import mock

AGENTS = dict(item.split("=", 1) for item in os.environ.get(
    "AGENTS", "stockholm=http://localhost:9001,helsinki=http://localhost:9002,vantaa=http://localhost:9003"
).split(","))
DSN = os.environ.get("TELEMETRY_DSN_TEMPLATE", "postgresql://agent_{node}:agent@localhost:5433/telemetry")
INCIDENT_TIMEOUT = float(os.environ.get("INCIDENT_TIMEOUT", "300"))  # a real model is slow
BUCKET, BUCKETS = 10, 30  # 10 s buckets, 5 minutes
# the repo, mounted at the same path as on the host so the fault scripts' compose calls
# resolve the same files. empty = no fault buttons, faults are run from a terminal.
REPO = os.environ.get("REPO_DIR", "")
FAULTS = {"surge": "faults/signup_surge.sh", "split": "faults/split.sh"}
active = dict.fromkeys(FAULTS, False)  # what this page switched on, a terminal can change it behind our back

B = "floor(extract(epoch FROM ts) / 10)::bigint * 10"
QUERIES = {
    ("requests", "failed", "peer_errors", "p95", "n_traces"):
        f"SELECT {B},"
        " count(*) FILTER (WHERE target = 'http'),"
        " count(*) FILTER (WHERE target = 'http' AND status = 'ERROR'),"
        " count(*) FILTER (WHERE target = 'peer' AND status = 'ERROR'),"
        " percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE target = 'http'),"
        " count(*) FROM traces WHERE ts >= %s GROUP BY 1",
    ("refused", "peer_unreachable", "n_logs"):
        f"SELECT {B},"
        " count(*) FILTER (WHERE code = '53300'),"
        " count(*) FILTER (WHERE code = 'PEER_UNREACHABLE'),"
        " count(*) FROM logs WHERE ts >= %s GROUP BY 1",
    ("connections", "max_connections", "n_metrics"):
        f"SELECT {B},"
        " max(value) FILTER (WHERE metric = 'db.connections'),"
        " min(value) FILTER (WHERE metric = 'db.connections.max'),"
        " count(*) FROM metrics WHERE ts >= %s GROUP BY 1",
}
# no row in a bucket means 0 for these, not unknown
COUNTS = {"requests", "failed", "peer_errors", "refused", "peer_unreachable", "n_traces", "n_logs", "n_metrics"}

ROWS = {
    "traces": "SELECT 'span:' || span_id AS id, ts, source, name, target, duration_ms, status FROM traces",
    "logs": "SELECT 'log:' || id AS id, ts, source, severity, code, left(message, 100) AS message FROM logs",
    "metrics": "SELECT 'metric:' || id AS id, ts, source, metric, value, unit FROM metrics",
}


def call(url, body=None, timeout=2):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def agent_status(url):
    try:
        return {"up": True, "health": call(f"{url}/health"), **call(f"{url}/state")}
    except (OSError, ValueError) as e:
        return {"up": False, "error": str(e)}


def telemetry(node, last):
    """Series per bucket for one machine, oldest first. `last` is the newest full bucket."""
    times = [last - BUCKET * i for i in reversed(range(BUCKETS))]
    series = {name: [0 if name in COUNTS else None] * BUCKETS for names in QUERIES for name in names}
    try:
        with psycopg.connect(DSN.format(node=node), connect_timeout=2) as conn:
            since = datetime.fromtimestamp(times[0], timezone.utc)
            for names, sql in QUERIES.items():
                for bucket, *values in conn.execute(sql, (since,)):
                    if bucket in times:
                        for name, value in zip(names, values):
                            if value is not None:
                                series[name][times.index(bucket)] = float(value)
    except psycopg.Error as e:
        return {"error": str(e).strip()}
    return series


def latest_rows(node):
    try:
        with psycopg.connect(DSN.format(node=node), connect_timeout=2, row_factory=dict_row) as conn:
            return {t: conn.execute(f"{sql} ORDER BY ts DESC LIMIT 8").fetchall() for t, sql in ROWS.items()}
    except psycopg.Error:
        return {t: [] for t in ROWS}


def expected():
    """The answer the mock gives per fault: scenario -> claim -> node -> confidence."""
    out = {}
    for scenario, by_node in mock.CLAIMS.items():
        claims = out[scenario] = {}
        for node, own in by_node.items():
            for text, confidence, _ in own:
                claims.setdefault(text, {})[node] = confidence
        for node, opinions in mock.OPINIONS.items():
            for text, confidence in opinions.items():
                if text in claims:
                    claims[text].setdefault(node, confidence)
    return out


pool = ThreadPoolExecutor(max_workers=12)
cache, cache_lock = {"at": 0, "value": None}, threading.Lock()
app = FastAPI()


@app.get("/")
def page():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/api/status")
def status():
    with cache_lock:  # several open tabs share one round of queries
        if time.time() - cache["at"] > 1.5:
            last = int(time.time() // BUCKET) * BUCKET - BUCKET
            agents = {n: pool.submit(agent_status, url) for n, url in AGENTS.items()}
            rows = {n: pool.submit(telemetry, n, last) for n in AGENTS}
            cache["value"] = {
                "now": time.time(), "bucket": BUCKET, "last": last,
                "agents": {n: f.result() for n, f in agents.items()},
                "telemetry": {n: f.result() for n, f in rows.items()},
                "expected": expected(),
                "faults": {"enabled": bool(REPO), "active": dict(active)},
            }
            cache["at"] = time.time()
        return cache["value"]


@app.get("/api/rows")
def rows():
    """The newest rows of each table, read machine by machine and put together here."""
    per_node = list(pool.map(latest_rows, AGENTS))
    return {t: sorted((r for n in per_node for r in n[t]), key=lambda r: r["ts"], reverse=True)[:14] for t in ROWS}


@app.post("/api/fault/{name}/{switch}")
def fault(name: str, switch: str):
    """Runs the same script a person would run in a terminal."""
    if not REPO:
        raise HTTPException(503, "fault buttons are off, REPO_DIR is not set")
    if name not in FAULTS or switch not in ("on", "off"):
        raise HTTPException(404, "unknown fault")
    cmd = [os.path.join(REPO, FAULTS[name])] + (["reset"] if switch == "off" else [])
    try:
        run = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, str(e))
    if run.returncode:
        raise HTTPException(500, (run.stderr or run.stdout).strip()[-400:])
    active[name] = switch == "on"
    cache["at"] = 0
    return {"fault": name, "on": active[name]}


@app.post("/api/incident")
def incident():
    body = {"id": f"ui-{time.time():.0f}"}

    def start(url):
        try:
            return call(f"{url}/incident", body, INCIDENT_TIMEOUT)
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    jobs = {n: pool.submit(start, url) for n, url in AGENTS.items()}
    return {"id": body["id"], "nodes": {n: f.result() for n, f in jobs.items()}}
