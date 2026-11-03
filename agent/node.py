"""One agent node. Every machine (stockholm, helsinki, vantaa) gets one. A node
reads only its own machine's telemetry and swaps beliefs with the other nodes.

    POST /incident  start on an incident: read own shard, add own claims
    POST /exchange  push-pull gossip: merge the caller's state, answer with ours
    GET  /state     current beliefs, digest, when each peer was last heard from
    GET  /health    liveness

Run: uvicorn node:app (belief.py from the repo root must be importable).
"""
import json
import os
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import mock
from belief import BeliefState, Claim
from shard import get_shard, sources

NODE_ID = os.environ.get("NODE_ID", "")
PEERS = os.environ.get("PEERS", "")  # "helsinki=http://agent-helsinki:8000,vantaa=..."
DSN = os.environ.get("TELEMETRY_DSN", "")  # empty = no DB, mock answers without evidence
MODE = os.environ.get("AGENT_MODE", "mock")
GOSSIP_SECONDS = float(os.environ.get("GOSSIP_SECONDS", "1"))  # 0 = only answer, never push
PEER_TIMEOUT = float(os.environ.get("PEER_TIMEOUT", "2"))
SHARD_LIMIT = int(os.environ.get("SHARD_LIMIT", "5000"))
WINDOW_SECONDS = int(os.environ.get("INCIDENT_WINDOW_SECONDS", "60"))

SCENARIOS = (None, "surge", "split", "none")
# what a malformed state from a peer can blow up with
BAD_STATE = (KeyError, TypeError, ValueError, AttributeError)


def make_agent(node, mode, scenario):
    # the real agent goes here later, with the same analyse/evaluate methods
    if mode == "mock":
        return mock.MockAgent(node, scenario)
    raise ValueError(f"unknown AGENT_MODE {mode!r}")


def parse_peers(value):
    peers = {}
    for item in filter(None, (p.strip() for p in value.split(","))):
        name, url = item.split("=", 1)
        peers[name] = url.rstrip("/")
    return peers


def parse_state(raw):
    # belief.py would turn "log:12" into a set of characters, so check the raw shape first
    for claim in raw.get("claims", raw).values():
        evidence = claim.get("evidence_ids", [])
        if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
            raise ValueError("evidence_ids must be a list of strings")
    return BeliefState.from_dict(raw)


def utc(dt):
    # telemetry ts is timestamptz, so a time without a zone is taken as UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def post(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class WrongIncident(Exception):
    pass


class Node:
    def __init__(self, node_id, peers=None, dsn="", mode="mock"):
        self.id = node_id
        self.peers = peers or {}
        self.dsn = dsn
        self.mode = mode
        self.lock = threading.Lock()
        self.unreachable = set()
        self._reset(None, None, make_agent(node_id, mode, "none"), None)

    @classmethod
    def from_env(cls):
        return cls(NODE_ID, parse_peers(PEERS), DSN, MODE)

    def _reset(self, incident, window, agent, shard):
        self.incident = incident
        self.window = window  # start, end and scenario hint, passed on so others can join
        self.agent = agent
        self.shard = shard
        self.state = BeliefState()
        self.scored = set()  # claim ids we already gave an opinion on, or chose not to
        self.last_seen = {}
        # start from the clock so a restarted node still beats its own old registers
        self.version = int(time.time() * 1000)

    def _add(self, text, confidence, evidence_ids):
        # version only has to grow per node, so one counter covers all our registers
        self.version += 1
        self.state.add_claim(Claim.create(text, evidence_ids, self.id, confidence, self.version))

    def cross_check(self):
        """Give our own confidence to claims other nodes made that we haven't scored yet."""
        with self.lock:
            incident, agent, shard = self.incident, self.agent, self.shard
            todo = [c.text for c in self.state.claims.values()
                    if self.id not in c.confidence and c.claim_id not in self.scored]
            self.scored.update(Claim.create(text).claim_id for text in todo)
        # no lock here, a real model can take seconds per claim and gossip has to go on
        opinions = [(text, agent.evaluate(text, shard)) for text in todo]
        with self.lock:
            if self.incident != incident:
                return
            for text, confidence in opinions:
                if confidence is not None:
                    self._add(text, confidence, [])

    def read_shard(self, start, end):
        if not self.dsn:
            return None
        shard = get_shard(self.dsn, start, end, SHARD_LIMIT)
        seen = sources(shard)
        if seen not in ([], [self.id]):
            # an admin DSN would make this one program with three connections, refuse it
            raise PermissionError(f"{self.id} can read rows of {seen}, check TELEMETRY_DSN")
        return shard

    def start_incident(self, incident, start, end, scenario=None):
        # read and analyse outside the lock, gossip keeps going meanwhile
        shard = self.read_shard(start, end)
        agent = make_agent(self.id, self.mode, scenario)
        hypotheses = agent.analyse(shard)
        window = {"start": start.isoformat(), "end": end.isoformat(), "scenario": scenario}
        with self.lock:
            if incident != self.incident:
                self._reset(incident, window, agent, shard)
            self.window, self.agent, self.shard = window, agent, shard
            for h in hypotheses:
                self._add(h["text"], h["confidence"], h["evidence_ids"])
        self.cross_check()
        return {
            "node": self.id,
            "incident": incident,
            "scenario": getattr(agent, "scenario", None),  # what the mock went with
            "claims": hypotheses,
            "records": {t: len(rows) for t, rows in shard.items()} if shard is not None else None,
            "sources": sources(shard) if shard is not None else None,
        }

    def exchange(self, incident, sender, state, window=None):
        """Merge a remote state into ours and return ours. A node with no incident
        (just restarted, or missed /incident) joins the sender's one if it got the window."""
        remote = parse_state(state)
        if self.incident is None and window:
            if window.get("scenario") not in SCENARIOS:
                raise ValueError(f"unknown scenario {window.get('scenario')!r}")
            self.start_incident(incident, utc(datetime.fromisoformat(window["start"])),
                                utc(datetime.fromisoformat(window["end"])), window.get("scenario"))
        with self.lock:
            if incident != self.incident:
                raise WrongIncident(f"{self.id} is on {self.incident!r}, not {incident!r}")
            merged = self.state.merge(remote)
            merged.to_dict()  # fails here, before we keep it, if something slipped through
            self.state = merged
            if sender:
                self.last_seen[sender] = time.time()
            return self.state.to_dict()

    def snapshot(self):
        with self.lock:
            return {
                "node": self.id,
                "incident": self.incident,
                "digest": self.state.digest(),
                "state": self.state.to_dict(),
                "peers": dict(self.last_seen),
            }

    def gossip_once(self):
        for name, url in self.peers.items():
            with self.lock:
                if self.incident is None:
                    return
                body = {"incident": self.incident, "from": self.id, "state": self.state.to_dict(),
                        "window": self.window}
            try:
                reply = post(f"{url}/exchange", body, PEER_TIMEOUT)
                self.exchange(reply["incident"], name, reply["state"])
            except (OSError, WrongIncident, *BAD_STATE) as e:
                # HTTPError is an OSError too, so a 409 from a peer on another incident lands here
                if name not in self.unreachable:
                    print(f"{self.id}: no exchange with {name} ({e!r})", file=sys.stderr, flush=True)
                self.unreachable.add(name)
                continue
            if name in self.unreachable:
                print(f"{self.id}: {name} is back", file=sys.stderr, flush=True)
                self.unreachable.discard(name)

    def gossip_forever(self, every):
        while True:
            time.sleep(every)
            try:
                self.cross_check()
                self.gossip_once()
            except Exception as e:  # a dead gossip thread would look like a partition
                print(f"{self.id}: gossip round failed ({e!r})", file=sys.stderr, flush=True)

    def db_ok(self):
        if not self.dsn:
            return None
        try:
            with psycopg.connect(self.dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False


class IncidentIn(BaseModel):
    id: str
    start: datetime | None = None
    end: datetime | None = None
    # mock only: forces the answer when there is no DB, otherwise the mock reads its rows
    scenario: Literal["surge", "split", "none"] | None = None


class ExchangeIn(BaseModel):
    incident: str
    sender: str = Field("", alias="from")
    state: dict
    window: dict | None = None


def make_app(node):
    @asynccontextmanager
    async def lifespan(app):
        if not node.id:
            raise RuntimeError("NODE_ID is not set")
        if GOSSIP_SECONDS > 0:  # also runs the cross-checks, so even a node without peers needs it
            threading.Thread(target=node.gossip_forever, args=(GOSSIP_SECONDS,),
                             daemon=True, name="gossip").start()
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"node": node.id, "mode": node.mode, "ok": True,
                "incident": node.incident, "db": node.db_ok()}

    @app.post("/incident")
    def incident(body: IncidentIn):
        end = utc(body.end) if body.end else datetime.now(timezone.utc)
        start = utc(body.start) if body.start else end - timedelta(seconds=WINDOW_SECONDS)
        if start >= end:
            raise HTTPException(422, "start must be before end")
        try:
            return node.start_incident(body.id, start, end, body.scenario)
        except psycopg.Error as e:
            raise HTTPException(503, f"telemetry db unavailable: {e}")
        except PermissionError as e:
            raise HTTPException(500, str(e))

    @app.post("/exchange")
    def exchange(body: ExchangeIn):
        try:
            state = node.exchange(body.incident, body.sender, body.state, body.window)
        except WrongIncident as e:
            raise HTTPException(409, str(e))
        except BAD_STATE as e:
            raise HTTPException(400, f"bad state: {e!r}")
        except psycopg.Error as e:  # joining an incident means reading our shard first
            raise HTTPException(503, f"telemetry db unavailable: {e}")
        except PermissionError as e:
            raise HTTPException(500, str(e))
        return {"node": node.id, "incident": body.incident, "state": state}

    @app.get("/state")
    def state():
        return node.snapshot()

    return app


app = make_app(Node.from_env())
