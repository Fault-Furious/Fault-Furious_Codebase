"""HTTP checks used by agent/check.sh. Stdlib only, so plain python3 runs it.

    verify.py health URL...                        every node answers /health
    verify.py down URL...                          nothing listens there any more
    verify.py isolation URL...                     each node only reads rows of its own source
    verify.py converge SCENARIO [--detect] [--real] [--start T --end T] URL...
    verify.py same URL...                          all on one incident and digest, e.g. after a restart

converge starts one incident on every node and waits until all of them hold the
same digest, every node has made the mock's claims for SCENARIO, every node that
has an opinion has scored every claim, and every node heard from both others.
--detect leaves the scenario out, so the mock has to find it in its own rows.
--real also demands evidence ids from the telemetry DB, not mock placeholders.
"""
import argparse
import json
import sys
import time
import urllib.request

import mock

TIMEOUT = 20
NODES = {"stockholm", "helsinki", "vantaa"}


def call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def wait_health(urls):
    deadline = time.time() + TIMEOUT
    for url in urls:
        while True:
            try:
                h = call(f"{url}/health")
                print(f"ok   {url} is {h['node']} (mode {h['mode']}, db {h['db']})")
                break
            except OSError:
                if time.time() > deadline:
                    fail(f"{url} not answering /health")
                time.sleep(0.5)


def check_down(urls):
    for url in urls:
        try:
            call(f"{url}/health")
        except OSError:
            print(f"ok   {url} is down")
            continue
        fail(f"{url} still answers")


def isolation(urls):
    for url in urls:
        r = call(f"{url}/incident", {"id": f"isolation-{time.time():.0f}", "scenario": "none"})
        total = sum(r["records"].values()) if r["records"] else 0
        if r["sources"] != [r["node"]] or total == 0:
            fail(f"{r['node']} read sources {r['sources']}, {r['records']}")
        print(f"ok   {r['node']} sees only {r['sources']}, {r['records']}")


def own_claims(scenario, node):
    return [text for text, _, _ in mock.CLAIMS.get(scenario, {}).get(node, [])]


def voters(scenario, text):
    """Nodes that should hold a register on this claim: who made it, plus who has an opinion."""
    made = {n for n in NODES if text in own_claims(scenario, n)}
    return made | {n for n in NODES if mock.OPINIONS.get(n, {}).get(text) is not None}


def settled(states):
    """Same incident and digest everywhere, and everyone heard from both others."""
    return (len({s["incident"] for s in states}) == 1 and len({s["digest"] for s in states}) == 1
            and all(set(s["peers"]) == NODES - {s["node"]} for s in states))


def converge(scenario, urls, detect=False, real=False, start=None, end=None):
    incident = f"check-{scenario}-{time.time():.0f}"
    body = {"id": incident}
    if not detect:
        body["scenario"] = scenario
    if start and end:
        body |= {"start": start, "end": end}
    replies = [call(f"{url}/incident", body) for url in urls]
    if sorted(r["node"] for r in replies) != sorted(NODES):
        fail(f"expected one node each of {sorted(NODES)}, got {[r['node'] for r in replies]}")

    for r in replies:
        made = [c["text"] for c in r["claims"]]
        if made != own_claims(scenario, r["node"]):
            fail(f"{r['node']} went with {r['scenario']!r} and claimed {made}, records {r['records']}")
        for claim in r["claims"]:
            if real and any(e.startswith("mock:") for e in claim["evidence_ids"]):
                fail(f"{r['node']} has no real evidence for {claim['text']!r} (records {r['records']})")
            print(f"ok   {r['node']} ({r['scenario']}) claims {claim['text']!r} {claim['confidence']}, "
                  f"evidence {claim['evidence_ids']}")

    expected = {text for n in NODES for text in own_claims(scenario, n)}
    deadline = time.time() + TIMEOUT
    while True:
        states = [call(f"{url}/state") for url in urls]
        claims = states[0]["state"].values()
        texts = {c["text"] for c in claims}
        scored = all(set(c["confidence"]) == voters(scenario, c["text"]) for c in claims)
        if settled(states) and texts == expected and scored:
            break
        if time.time() > deadline:
            fail(f"{scenario}: no convergence, digests {sorted({s['digest'][:12] for s in states})}, "
                 f"claims {texts}, peers {[(s['node'], sorted(s['peers'])) for s in states]}")
        time.sleep(0.5)

    for c in claims:
        votes = ", ".join(f"{n} {v['value']}" for n, v in sorted(c["confidence"].items()))
        print(f"ok   {scenario}: {c['text']!r} [{votes}] evidence {len(c['evidence_ids'])}")
    print(f"ok   {scenario}: {len(urls)} nodes on digest {states[0]['digest'][:12]}")


def same(urls):
    deadline = time.time() + TIMEOUT
    while True:
        try:
            states = [call(f"{url}/state") for url in urls]
            if settled(states):
                break
        except OSError:
            states = []
        if time.time() > deadline:
            fail(f"nodes did not settle: {[(s['node'], s['incident'], s['digest'][:12]) for s in states]}")
        time.sleep(0.5)
    print(f"ok   all on {states[0]['incident']}, digest {states[0]['digest'][:12]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["health", "down", "isolation", "converge", "same"])
    p.add_argument("args", nargs="+")
    p.add_argument("--detect", action="store_true")
    p.add_argument("--real", action="store_true")
    p.add_argument("--start")
    p.add_argument("--end")
    a = p.parse_intermixed_args()
    if a.cmd == "converge":
        converge(a.args[0], a.args[1:], a.detect, a.real, a.start, a.end)
    else:
        {"health": wait_health, "down": check_down, "isolation": isolation, "same": same}[a.cmd](a.args)


if __name__ == "__main__":
    main()
