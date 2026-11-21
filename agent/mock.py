"""Fake agent with hardcoded answers, so the rest of the team can build against
node.py before any model is involved.

Same methods the real agent will have:
    analyse(shard)          -> [{"text", "confidence", "evidence_ids"}, ...]
    evaluate(claim, shard)  -> confidence for someone else's claim, or None to abstain

The claims are fixed, but which ones a node makes comes from its own rows when it
has a shard, so a real fault gets the right answer without being told. The
scenario argument overrides that, for running without a DB.

Claim texts are shared constants on purpose. belief.py keys claims by a hash of
the text, so two agents only agree on a claim if they say it the same way.
"""
EVIDENCE_LIMIT = 5

DB_REFUSING = "userdb is refusing connections, max_connections reached (53300)"
SIGNUP_FAILING = "signup inserts into userdb are failing"
PEERS_CUT = "stockholm and helsinki cannot reach each other"
DB_HEALTHY = "userdb is healthy, no connection errors"


def _ids(rows, match):
    return [r["id"] for r in rows if match(r)][:EVIDENCE_LIMIT]


def _refusals(shard):
    return (_ids(shard["logs"], lambda r: r["code"] == "53300")
            + _ids(shard["metrics"], lambda r: r["metric"] == "db.connections.refused" and r["value"] > 0))


def _db_errors(shard):
    # 23505 is just "username taken", loadgen hits it on every start
    return (_ids(shard["traces"], lambda r: r["target"] == "userdb" and r["status"] == "ERROR"
                 and (r.get("attrs") or {}).get("sqlstate") != "23505")
            + _ids(shard["logs"], lambda r: r["severity"] == "ERROR" and r["code"] != "23505"))


def _peer_errors(shard):
    return (_ids(shard["logs"], lambda r: r["code"] == "PEER_UNREACHABLE")
            + _ids(shard["traces"], lambda r: r["target"] == "peer" and r["status"] == "ERROR"))


def _db_stats(shard):
    return _ids(shard["metrics"], lambda r: r["metric"] == "db.connections")


def detect(node, shard):
    """Which scenario this node's own rows look like. Vantaa can't see a split, to
    it a split looks like a quiet day."""
    if node == "vantaa":
        return "surge" if _refusals(shard) or _db_errors(shard) else "none"
    if _db_errors(shard):
        return "surge"
    return "split" if _peer_errors(shard) else "none"


# scenario -> node -> what that node claims on its own: (text, confidence, evidence picker)
CLAIMS = {
    "surge": {
        "vantaa": [(DB_REFUSING, 0.9, _refusals)],
        "stockholm": [(SIGNUP_FAILING, 0.8, _db_errors)],
        "helsinki": [(SIGNUP_FAILING, 0.8, _db_errors)],
    },
    "split": {
        "vantaa": [(DB_HEALTHY, 0.9, _db_stats)],
        "stockholm": [(PEERS_CUT, 0.8, _peer_errors)],
        "helsinki": [(PEERS_CUT, 0.8, _peer_errors)],
    },
    "none": {
        "vantaa": [(DB_HEALTHY, 0.9, _db_stats)],
    },
}

# how much a node believes a claim it didn't make itself, missing = abstain.
# the apps' own errors carry the db's "too many clients" text, so they back the
# cause strongly, while vantaa only half backs the symptom: it never sees the
# signup traffic, just the refusals. that keeps the cause on top after merging.
OPINIONS = {
    "vantaa": {SIGNUP_FAILING: 0.5},  # it can't see the link between the apps at all
    "stockholm": {DB_REFUSING: 0.8, DB_HEALTHY: 0.7},
    "helsinki": {DB_REFUSING: 0.8, DB_HEALTHY: 0.7},
}


class MockAgent:
    def __init__(self, node, scenario=None):
        self.node = node
        self.scenario = scenario

    def analyse(self, shard):
        if self.scenario is None:
            self.scenario = detect(self.node, shard) if shard else "none"
        out = []
        for text, confidence, pick in CLAIMS.get(self.scenario, {}).get(self.node, []):
            evidence = pick(shard) if shard else []
            out.append({"text": text, "confidence": confidence,
                        "evidence_ids": evidence or [f"mock:{self.node}-1"]})
        return out

    def evaluate(self, claim, shard):
        return OPINIONS.get(self.node, {}).get(claim)
