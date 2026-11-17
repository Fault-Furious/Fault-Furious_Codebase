"""HTTP checks used by agent/check.sh. Stdlib only, so plain python3 runs it.

    verify.py health URL...                        every node answers /health
    verify.py down URL...                          nothing listens there any more
    verify.py isolation URL...                     each node only reads rows of its own source
    verify.py converge SCENARIO [--detect] [--real] [--llm] [--start T --end T] URL...
    verify.py start SCENARIO [--detect] [--start T --end T] URL...   only post the incident
    verify.py same [--judge SCENARIO] [--down NODE] URL...   all on one incident and digest
    verify.py partial [--judge SCENARIO] URL...    the nodes given agree, the missing one is stale
    verify.py model URL [up|down]                  /health says the model is up or down

converge starts one incident on every node and waits until all of them hold the
same digest, every node has made the mock's claims for SCENARIO, every node that
has an opinion has scored every claim, and every node heard from both others.
--detect leaves the scenario out, so the mock has to find it in its own rows.
--real also demands evidence ids from the telemetry DB, not mock placeholders.
--llm is for model nodes: their claims can't be listed up front, so instead each
machine has to back what it can see and nothing wrong may get a mean of 0.5 or
more (see judge). --down names a node whose model is known to be gone.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import llm
import mock

# a model node takes minutes, not seconds, so llm runs raise both
TIMEOUT = float(os.environ.get("VERIFY_TIMEOUT", "20"))
CALL_TIMEOUT = float(os.environ.get("VERIFY_CALL_TIMEOUT", "10"))
NODES = {"stockholm", "helsinki", "vantaa"}

# what a model swarm has to get right per fault: each claim here backed (0.5 or
# more) by the machines that can see it, so no single machine has the answer alone
REQUIRED = {
    "surge": {mock.DB_REFUSING: {"vantaa"}, mock.SIGNUP_FAILING: {"stockholm", "helsinki"}},
    "split": {mock.PEERS_CUT: {"stockholm", "helsinki"}, mock.DB_HEALTHY: {"vantaa"}},
    "none": {mock.DB_HEALTHY: {"vantaa"}},
}
# and these must not end up with a mean of 0.5 or more
WRONG = {
    "surge": {mock.DB_HEALTHY, mock.PEERS_CUT, llm.APPS_OK, llm.DB_SLOW,
              llm.STOCKHOLM_DOWN, llm.HELSINKI_DOWN},
    "split": {mock.DB_REFUSING, mock.SIGNUP_FAILING, llm.APPS_OK, llm.DB_SLOW,
              llm.STOCKHOLM_DOWN, llm.HELSINKI_DOWN},
    "none": {mock.DB_REFUSING, mock.SIGNUP_FAILING, mock.PEERS_CUT, llm.DB_SLOW,
             llm.STOCKHOLM_DOWN, llm.HELSINKI_DOWN},
}


def call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
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


def settled(states, nodes=NODES, down=()):
    """Same incident and digest everywhere, everyone heard from all the others in
    nodes, and nobody still owes work or had its model fail, except the nodes in down."""
    return (len({s["incident"] for s in states}) == 1 and len({s["digest"] for s in states}) == 1
            and all(set(s["peers"]) >= nodes - {s["node"]} for s in states)
            and all(not (s.get("pending") or s.get("error")) or s["node"] in down for s in states))


def mean(claim):
    values = [r["value"] for r in claim["confidence"].values()]
    return sum(values) / len(values) if values else 0


def judge(scenario, claims, down=()):
    """What is wrong with a model swarm's final state for this fault, empty if nothing.
    Nodes in down (dead, cut off, no model) aren't expected to back anything."""
    problems = []
    by_text = {c["text"]: c for c in claims}
    for text, backers in REQUIRED[scenario].items():
        need = backers - set(down)
        if not need:
            continue
        c = by_text.get(text)
        if c is None:
            problems.append(f"nobody claimed {text!r}")
            continue
        backed = {n for n, r in c["confidence"].items() if r["value"] >= 0.5}
        if mean(c) < 0.5:
            problems.append(f"{text!r} only has a mean of {mean(c):.2f}")
        if not need <= backed:
            problems.append(f"{text!r} is not backed by {sorted(need - backed)}")
    for c in claims:
        if c["text"] in WRONG[scenario] and mean(c) >= 0.5:
            problems.append(f"wrong claim {c['text']!r} has a mean of {mean(c):.2f}")
        if not c["evidence_ids"] or any(e.startswith("mock:") for e in c["evidence_ids"]):
            problems.append(f"{c['text']!r} has no real evidence: {c['evidence_ids']}")
    return problems


def show(scenario, claims):
    for c in sorted(claims, key=mean, reverse=True):
        votes = ", ".join(f"{n} {v['value']}" for n, v in sorted(c["confidence"].items()))
        print(f"ok   {scenario}: {mean(c):.2f} {c['text']!r} [{votes}] evidence {len(c['evidence_ids'])}")


def start_incident(scenario, urls, detect=False, start=None, end=None):
    incident = f"check-{scenario}-{time.time():.0f}"
    body = {"id": incident}
    if not detect:
        body["scenario"] = scenario
    if start and end:
        body |= {"start": start, "end": end}
    # all at once, like a real alert would, a model node takes a while to answer
    with ThreadPoolExecutor(len(urls)) as pool:
        replies = list(pool.map(lambda url: call(f"{url}/incident", body), urls))
    for r in replies:
        if r.get("error"):
            print(f"..   {r['node']} has no model: {r['error']}")
        for claim in r["claims"]:
            print(f"ok   {r['node']} ({r['scenario']}) claims {claim['text']!r} {claim['confidence']}, "
                  f"evidence {claim['evidence_ids']}")
    return replies


def converge(scenario, urls, detect=False, real=False, start=None, end=None, model=False):
    replies = start_incident(scenario, urls, detect, start, end)
    if sorted(r["node"] for r in replies) != sorted(NODES):
        fail(f"expected one node each of {sorted(NODES)}, got {[r['node'] for r in replies]}")

    for r in replies:
        made = [c["text"] for c in r["claims"]]
        if model and r.get("error"):
            fail(f"{r['node']} could not ask its model: {r['error']}")
        if not model and made != own_claims(scenario, r["node"]):
            fail(f"{r['node']} went with {r['scenario']!r} and claimed {made}, records {r['records']}")
        for claim in r["claims"]:
            if real and any(e.startswith("mock:") for e in claim["evidence_ids"]):
                fail(f"{r['node']} has no real evidence for {claim['text']!r} (records {r['records']})")

    expected = {text for n in NODES for text in own_claims(scenario, n)}
    deadline = time.time() + TIMEOUT
    while True:
        states = [call(f"{url}/state") for url in urls]
        claims = list(states[0]["state"].values())
        texts = {c["text"] for c in claims}
        if model:
            done = settled(states)
        else:
            scored = all(set(c["confidence"]) == voters(scenario, c["text"]) for c in claims)
            done = settled(states) and texts == expected and scored
        if done:
            break
        if time.time() > deadline:
            fail(f"{scenario}: no convergence, digests {sorted({s['digest'][:12] for s in states})}, "
                 f"claims {texts}, peers {[(s['node'], sorted(s['peers'])) for s in states]}, "
                 f"pending {[(s['node'], s.get('pending'), s.get('error')) for s in states]}")
        time.sleep(0.5)

    show(scenario, claims)
    if model:
        for problem in judge(scenario, claims):
            fail(f"{scenario}: {problem}")
    print(f"ok   {scenario}: {len(urls)} nodes on digest {states[0]['digest'][:12]}")


def wait_settled(urls, nodes, down=()):
    deadline = time.time() + TIMEOUT
    while True:
        try:
            states = [call(f"{url}/state") for url in urls]
            if settled(states, nodes, down):
                return states
        except OSError:
            states = []
        if time.time() > deadline:
            where = [(s["node"], s["incident"], s["digest"][:12], s.get("pending"), s.get("error"))
                     for s in states]
            fail(f"nodes did not settle: {where}")
        time.sleep(0.5)


def check(scenario, states, down=()):
    if scenario:
        claims = list(states[0]["state"].values())
        show(scenario, claims)
        for problem in judge(scenario, claims, down):
            fail(f"{scenario}: {problem}")


def same(urls, scenario=None, down=()):
    states = wait_settled(urls, NODES, down)
    check(scenario, states, down)
    print(f"ok   all on {states[0]['incident']}, digest {states[0]['digest'][:12]}")


STALE = 5  # gossip runs every second, a peer not heard from for this long is gone


def partial(urls, scenario=None):
    """The nodes given agree with each other while the rest of NODES is gone."""
    here = {call(f"{url}/state")["node"] for url in urls}
    gone = NODES - here
    deadline = time.time() + TIMEOUT
    while True:
        states = wait_settled(urls, here)
        seen = {n: max((s["peers"].get(n, 0) for s in states), default=0) for n in gone}
        if all(time.time() - t > STALE for t in seen.values()):
            break
        if time.time() > deadline:
            fail(f"{sorted(gone)} should be gone but were heard from {seen}")
        time.sleep(1)
    for n, t in seen.items():
        print(f"ok   {n} last heard from {f'{time.time() - t:.0f}s ago' if t else 'never'}")
    check(scenario, states, gone)
    print(f"ok   {sorted(here)} agree without {sorted(gone)}, digest {states[0]['digest'][:12]}")


def model(url, want):
    deadline = time.time() + TIMEOUT
    while True:
        h = call(f"{url}/health")
        if h["model"] is (want == "up"):
            print(f"ok   {h['node']} model is {want}")
            return
        if time.time() > deadline:
            fail(f"{h['node']} model should be {want}, /health says {h['model']}")
        time.sleep(0.5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["health", "down", "isolation", "converge", "start", "same",
                                   "partial", "model"])
    p.add_argument("args", nargs="+")
    p.add_argument("--detect", action="store_true")
    p.add_argument("--real", action="store_true")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--judge")
    p.add_argument("--down", action="append", default=[])
    p.add_argument("--start")
    p.add_argument("--end")
    a = p.parse_intermixed_args()
    if a.cmd == "converge":
        converge(a.args[0], a.args[1:], a.detect, a.real, a.start, a.end, a.llm)
    elif a.cmd == "start":
        start_incident(a.args[0], a.args[1:], a.detect, a.start, a.end)
    elif a.cmd == "same":
        same(a.args, a.judge, a.down)
    elif a.cmd == "partial":
        partial(a.args, a.judge)
    elif a.cmd == "model":
        model(a.args[0], a.args[1] if len(a.args) > 1 else "up")
    else:
        {"health": wait_health, "down": check_down, "isolation": isolation}[a.cmd](a.args)


if __name__ == "__main__":
    main()
