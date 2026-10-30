import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from html import escape

import psycopg
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import otel

USER_DSN = os.environ["USER_DB_DSN"]
DB_TIMEOUT = int(os.environ.get("USER_DB_CONNECT_TIMEOUT", "3"))
PEER_URL = os.environ.get("PEER_URL", "")  # the other app server; empty = no federated search
PEER_TIMEOUT = float(os.environ.get("PEER_TIMEOUT", "2"))
# ponytail: low on purpose so CPU doesn't cap signups before the DB does; raise for anything real
HASH_ITERATIONS = int(os.environ.get("PASSWORD_HASH_ITERATIONS", "1000"))
PAGE_SIZE = 100

app = FastAPI()


@app.middleware("http")
async def trace_request(request: Request, call_next):
    # ponytail: route templates not used as span name until /{forum} cardinality matters
    with otel.start_span(f"{request.method} {request.url.path}", target="http",
                         parent=otel.parse_traceparent(request.headers.get("traceparent")),
                         device=request.headers.get("user-agent", "")) as span:
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:
            otel.log("ERROR", f"unhandled {type(e).__name__}: {e}")
            raise
        span.attrs["status_code"] = response.status_code
        if response.status_code >= 500:
            span.status = "ERROR"
        otel.metric("http.server.duration", (time.perf_counter() - t0) * 1000, "ms",
                    path=request.url.path, status_code=response.status_code)
        return response


def query(name, stmt, params=(), fetch=True):
    """One SQL call = one child span. Connect-per-call is deliberate: a surge
    must hit the DB's max_connections for real (SQLSTATE 53300)."""
    with otel.start_span(name, target="userdb") as span:
        try:
            with psycopg.connect(USER_DSN, connect_timeout=DB_TIMEOUT) as conn:
                cur = conn.execute(stmt, params)
                return cur.fetchall() if fetch else None
        except psycopg.Error as e:
            code = getattr(e, "sqlstate", None)
            span.attrs["sqlstate"] = code
            otel.log("ERROR", f"{name} failed: {e}".strip(), code=code)
            raise


ONLINE_WINDOW = 300
_online = {}  # username -> last login time, this server only


def online_here():
    cutoff = time.time() - ONLINE_WINDOW
    return sum(t > cutoff for t in _online.values())


@app.get("/api/online")
async def api_online():  # async: answers from the event loop even when the worker threads are saturated
    return {"online": online_here()}


def online_in_peer():
    """Ask the other region how many users it has online. Carries the trace across servers."""
    with otel.start_span("peer online", target="peer", url=PEER_URL) as span:
        req = urllib.request.Request(f"{PEER_URL}/api/online", headers={"traceparent": otel.traceparent()})
        try:
            with urllib.request.urlopen(req, timeout=PEER_TIMEOUT) as r:
                return json.load(r)["online"]
        except OSError as e:
            span.status = "ERROR"
            span.attrs["error"] = type(e).__name__
            otel.log("WARN", f"peer {PEER_URL} unreachable: {e}", code="PEER_UNREACHABLE")
            return None


def hash_pw(password, salt=None, iterations=HASH_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"{iterations}${salt}${digest}"


def check_pw(password, stored):
    iterations, salt, _ = stored.split("$", 2)
    return hmac.compare_digest(hash_pw(password, salt, int(iterations)), stored)


if __name__ == "__main__":
    h = hash_pw("pw")
    assert check_pw("pw", h) and not check_pw("bad", h)
    assert check_pw("pw", hash_pw("pw", iterations=5))  # survives changing HASH_ITERATIONS
    print("ok")


def page(title, body, status=200):
    return HTMLResponse(f"<!doctype html><title>{escape(title)}</title><h1>{escape(title)}</h1>{body}", status_code=status)


@app.exception_handler(psycopg.Error)
async def db_down(request, e):
    return page("Service unavailable", "<p>Database error, try again.</p>", 503)


@app.get("/")
def index(q: str = ""):
    rows = []
    if q:
        rows = query("search messages",
                     "SELECT id, username, message FROM messages WHERE message ILIKE %s ORDER BY id DESC LIMIT %s",
                     (f"%{q}%", PAGE_SIZE))
    peer = online_in_peer() if PEER_URL else None
    items = "".join(f"<li><b>{escape(u)}</b>: {escape(m)}</li>" for _, u, m in rows)
    return page("Nordic Forum",
                f'<p>Online here: {online_here()}, other region: {"?" if peer is None else peer}</p><form><input name="q" value="{escape(q)}"><button>Search</button></form>'
                f'<a href="/login">Log in</a> | <a href="/signup">Sign up</a><ul>{items}</ul>')


@app.get("/login")
def login_form():
    return page("Log in", '<form method="post"><input name="username"><input name="password" type="password"><button>Log in</button></form>')


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    rows = query("select user", "SELECT password FROM users WHERE username = %s", (username,))
    ok = bool(rows) and check_pw(password, rows[0][0])
    otel.annotate(username=username)
    otel.log("INFO" if ok else "WARN", f"login {'ok' if ok else 'failed'} for {username}")
    otel.metric("login.attempts", 1, "count", ok=ok)
    if not ok:
        return page("Log in", "<p>Wrong username or password.</p>", 401)
    _online[username] = time.time()
    return RedirectResponse("/", status_code=303)


@app.get("/signup")
def signup_form():
    return page("Sign up", '<form method="post"><input name="username"><input name="email" type="email"><input name="password" type="password"><button>Sign up</button></form>')


@app.post("/signup")
def signup(username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        query("insert user", "INSERT INTO users (username, password, email) VALUES (%s, %s, %s)",
              (username, hash_pw(password), email), fetch=False)
    except psycopg.errors.UniqueViolation:
        return page("Sign up", "<p>Username taken.</p>", 409)
    otel.log("INFO", f"signup {username}")
    otel.metric("signups", 1, "count")
    return RedirectResponse("/login", status_code=303)


@app.get("/{forum}")
def forum(forum: str, page_no: int = 1):
    otel.annotate(forum=forum, page=page_no)
    # ponytail: messages has no forum column in the agreed schema, so every forum shows the same feed
    rows = query("select messages",
                 "SELECT id, username, message FROM messages ORDER BY id DESC LIMIT %s OFFSET %s",
                 (PAGE_SIZE, (max(page_no, 1) - 1) * PAGE_SIZE))
    items = "".join(f"<li><b>{escape(u)}</b>: {escape(m)}</li>" for _, u, m in rows)
    return page(f"Forum: {forum}", f'<ul>{items}</ul><a href="?page_no={page_no + 1}">Next</a>')
