"""Fake visitors. Steady browsing on every target; optional signup flood for the surge fault.

BASE_RPS   requests/second per target of normal traffic (pages, search, login)
SIGNUP_RPS requests/second per target of POST /signup (0 normally; the surge fault raises it)
"""
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TARGETS = [t for t in os.environ["TARGETS"].split(",") if t]
BASE_RPS = float(os.environ.get("BASE_RPS", "2"))
SIGNUP_RPS = float(os.environ.get("SIGNUP_RPS", "0"))
TIMEOUT = float(os.environ.get("LOADGEN_TIMEOUT", "10"))
FORUMS = os.environ.get("FORUMS", "general,hockey,sauna,saunalahti,fika").split(",")
AGENTS = ["Mozilla/5.0 (iPhone)", "Mozilla/5.0 (Android)", "Mozilla/5.0 (Windows NT 10.0)", "Mozilla/5.0 (Macintosh)"]
USERS = [f"visitor{i}" for i in range(20)]
PASSWORD = "visitor-pw"


def hit(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": random.choice(AGENTS)})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def browse(base):
    return random.choice([
        lambda: hit(f"{base}/"),
        lambda: hit(f"{base}/?q=" + random.choice(["hei", "maailma", "1"])),
        lambda: hit(f"{base}/{random.choice(FORUMS)}?page_no={random.randint(1, 3)}"),
        lambda: hit(f"{base}/login", {"username": random.choice(USERS), "password": PASSWORD}),
    ])()


def signup(base):
    return hit(f"{base}/signup", {"username": "u" + secrets.token_hex(6),
                                  "email": "x@example.fi", "password": "pw"})


def loop(fn, base, rps):
    # one thread per request so a slow server can't throttle the offered load
    while True:
        threading.Thread(target=fn, args=(base,), daemon=True).start()
        time.sleep(random.expovariate(rps))


def main():
    for base in TARGETS:  # make sure the browsing users exist; 409 = already there
        for u in USERS:
            hit(f"{base}/signup", {"username": u, "email": f"{u}@example.fi", "password": PASSWORD})
    for base in TARGETS:
        if BASE_RPS > 0:
            threading.Thread(target=loop, args=(browse, base, BASE_RPS), daemon=True).start()
        if SIGNUP_RPS > 0:
            threading.Thread(target=loop, args=(signup, base, SIGNUP_RPS), daemon=True).start()
    print(f"loadgen: {TARGETS} base={BASE_RPS}/s signup={SIGNUP_RPS}/s", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
