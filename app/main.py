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


STYLE = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#1f2a33;--muted:#64717d;--line:#dde3e8;--accent:#2f6f8f;--accent-ink:#fff;--soft:#e8f0f4;--warn:#9a3b2f;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141a1f;--card:#1c242b;--ink:#e6ecf0;--muted:#93a1ad;--line:#2c3740;--accent:#7fb6d1;--accent-ink:#10181d;--soft:#22303a;--warn:#e59482;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
header{background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:46rem;margin:0 auto;padding:.9rem 1rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:.02em;text-decoration:none;color:var(--ink)}
.brand span{color:var(--accent)}
main{max-width:46rem;margin:0 auto;padding:2rem 1rem 3rem}
h1{font-size:1.75rem;margin:0 0 1.25rem;letter-spacing:-.01em}
p{margin:0 0 1rem}
.stats{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.25rem}
.pill{background:var(--soft);color:var(--ink);border-radius:999px;padding:.25rem .8rem;font-size:.875rem}
.pill b{color:var(--accent)}
form{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 1.25rem}
form.stack{flex-direction:column;max-width:22rem;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.25rem}
label{font-size:.875rem;color:var(--muted);display:flex;flex-direction:column;gap:.3rem}
input{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.55rem .75rem;flex:1;min-width:0}
input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
button,.btn{font:inherit;font-weight:600;background:var(--accent);color:var(--accent-ink);border:0;border-radius:8px;padding:.55rem 1.1rem;cursor:pointer;text-decoration:none;display:inline-block}
button:hover,.btn:hover{filter:brightness(1.08)}
.links{display:flex;gap:.75rem;margin-bottom:1.5rem}
.links a{text-decoration:none;font-weight:500}
ul.feed{list-style:none;margin:0 0 1.5rem;padding:0;display:flex;flex-direction:column;gap:.6rem}
ul.feed li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.75rem 1rem;overflow-wrap:anywhere}
ul.feed li b{display:block;font-size:.85rem;color:var(--accent);margin-bottom:.15rem}
.notice{background:var(--card);border-left:4px solid var(--warn);border-radius:6px;padding:.75rem 1rem}
"""


def page(title, body, status=200):
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>"
        f'<header><div class="bar"><a class="brand" href="/">Nordic <span>Forum</span></a>'
        f'<nav class="links"><a href="/login">Log in</a><a href="/signup">Sign up</a></nav></div></header>'
        f"<main><h1>{escape(title)}</h1>{body}</main></body></html>",
        status_code=status)


def feed(rows):
    return '<ul class="feed">' + "".join(f"<li><b>{escape(u)}</b>{escape(m)}</li>" for _, u, m in rows) + "</ul>"


@app.exception_handler(psycopg.Error)
async def db_down(request, e):
    return page("Service unavailable", '<p class="notice">Database error, try again.</p>', 503)


@app.get("/")
def index(q: str = ""):
    rows = []
    if q:
        rows = query("search messages",
                     "SELECT id, username, message FROM messages WHERE message ILIKE %s ORDER BY id DESC LIMIT %s",
                     (f"%{q}%", PAGE_SIZE))
    peer = online_in_peer() if PEER_URL else None
    return page("Nordic Forum",
                f'<div class="stats"><span class="pill">Online here: <b>{online_here()}</b></span>'
                f'<span class="pill">Other region: <b>{"?" if peer is None else peer}</b></span></div>'
                f'<form><input name="q" value="{escape(q)}" placeholder="Search messages…" aria-label="Search messages"><button>Search</button></form>'
                f'{feed(rows)}')


@app.get("/login")
def login_form():
    return page("Log in", '<form method="post" class="stack">'
                          '<label>Username<input name="username" autocomplete="username"></label>'
                          '<label>Password<input name="password" type="password" autocomplete="current-password"></label>'
                          '<button>Log in</button></form>')


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    rows = query("select user", "SELECT password FROM users WHERE username = %s", (username,))
    ok = bool(rows) and check_pw(password, rows[0][0])
    otel.annotate(username=username)
    otel.log("INFO" if ok else "WARN", f"login {'ok' if ok else 'failed'} for {username}")
    otel.metric("login.attempts", 1, "count", ok=ok)
    if not ok:
        return page("Log in", '<p class="notice">Wrong username or password.</p>', 401)
    _online[username] = time.time()
    return RedirectResponse("/", status_code=303)


@app.get("/signup")
def signup_form():
    return page("Sign up", '<form method="post" class="stack">'
                           '<label>Username<input name="username" autocomplete="username"></label>'
                           '<label>Email<input name="email" type="email" autocomplete="email"></label>'
                           '<label>Password<input name="password" type="password" autocomplete="new-password"></label>'
                           '<button>Sign up</button></form>')


@app.post("/signup")
def signup(username: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        query("insert user", "INSERT INTO users (username, password, email) VALUES (%s, %s, %s)",
              (username, hash_pw(password), email), fetch=False)
    except psycopg.errors.UniqueViolation:
        return page("Sign up", '<p class="notice">Username taken.</p>', 409)
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
    return page(f"Forum: {forum}", f'{feed(rows)}<a class="btn" href="?page_no={page_no + 1}">Next</a>')
