"""Real agent: a local model (Ollama) reads a summary of this node's own rows and
picks claims from a fixed list. Same two methods as the mock:

    analyse(shard)          -> [{"text", "confidence", "evidence_ids", "reason"}, ...]
    evaluate(claim, shard)  -> confidence for someone else's claim, or None to abstain

The model can only pick texts from CLAIMS. belief.py merges claims by a hash of
the text, so free text from three models would never meet. It can only cite ids
that are in its summary, and those are rows of its own shard.

When the model can't be reached we retry once and then abstain, no fallback to
the mock. evaluate raises ModelDown instead, so the node can ask again later.
"""
import http.client
import json
import os
import re
import sys
import urllib.request
from collections import Counter, defaultdict

import mock

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))  # three nodes queue on one ollama
LLM_RETRIES = int(os.environ.get("LLM_RETRIES", "1"))
LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "8192"))

EVIDENCE_LIMIT = 5
MAX_CLAIMS = 4
MIN_CERTAINTY = 0.5  # below that it's not our claim, other nodes would just waste calls on it
GROUPS = 12  # per table, the rest is just counted
SAMPLE_IDS = 3
MESSAGE_CHARS = 160

# the four the mock uses, so mock and model nodes still agree on wording, plus
# a few more so the model has to choose. APPS_OK is only right on a quiet day
APPS_OK = "no request to or from the app servers is failing"
DB_SLOW = "userdb queries take over 1000 ms"  # same unit as the summary, or an 8b model mixes them up
STOCKHOLM_DOWN = "the stockholm app server is down, not serving any requests"
HELSINKI_DOWN = "the helsinki app server is down, not serving any requests"
# sorted, so the right answers aren't all at the top of the list the model reads
CLAIMS = sorted([mock.DB_REFUSING, mock.SIGNUP_FAILING, mock.PEERS_CUT, mock.DB_HEALTHY,
                 APPS_OK, DB_SLOW, STOCKHOLM_DOWN, HELSINKI_DOWN])

# codes the whole team knows are noise, loadgen hits 23505 on every start
KNOWN_CODES = {"23505": "username already taken, normal"}

SEES = {
    "stockholm": "traces, logs and metrics of the stockholm app server",
    "helsinki": "traces, logs and metrics of the helsinki app server",
    "vantaa": "the postgres log and connection metrics of userdb, nothing from the app servers",
}

SYSTEM = """You are the on-call agent for the machine {node} of a small forum platform.

The platform:
- stockholm and helsinki are two app servers running the same Python code. Each serves /, /login, /signup and /{{forum}}.
- both store users in userdb, a PostgreSQL database on vantaa.
- to show who is online, each app server also calls the other one over HTTP (spans named "peer online").

You can only see {sees}. {node} itself is up, it is what wrote the telemetry
below. Two other agents look at the other machines and will
compare notes with you, so judge only from your own telemetry and never guess
what the other machines see. Your own calls to another machine and the errors
they get are data about it, but they only show what you get back: a call that
can't connect means you can't reach it, not that it is down.
Be skeptical: most claims are false for any one incident, and a claim only counts as
supported when a line of your summary shows it."""

ANALYSE = """Telemetry summary for {node}:

{summary}

Which of these claims do your own rows support? List at most {most}, the one
you believe most first. For each one:
- reason: one short sentence naming the line of the summary that shows it
- verdict: "supported" if your rows show it, "contradicted" if they show the
  opposite, "cannot see" if your machine has no data about it
- certainty: how sure you are of that verdict, 0 to 1
- evidence_ids: up to {limit} ids from that line
Return an empty list if none of them fits.

Claims:
{claims}"""

EVALUATE = """Telemetry summary for {node}:

{summary}

Another agent claims: "{claim}"

Answer with:
- visible: does your telemetry have any data about what this claim is about? Your
  own calls to a machine count. false only if nothing in your summary touches it
- reason: one short sentence naming the line of your summary that shows it, or why you can't tell
- verdict: "supported" if your rows show it, "contradicted" if they show the
  opposite, "cannot see" if your machine has no data about it
- certainty: how sure you are of that verdict, 0 to 1"""

# asking for the verdict before the number keeps a small model from writing
# "it can reach helsinki" and then 0.8 for "cannot reach each other". the number
# is how sure it is of the verdict, score() turns both into one confidence
VERDICTS = ["supported", "contradicted", "cannot see"]

EVALUATE_SCHEMA = {
    "type": "object",
    "properties": {
        "visible": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 200},
        "verdict": {"type": "string", "enum": VERDICTS},
        "certainty": {"type": "number"},
    },
    "required": ["visible", "reason", "verdict", "certainty"],
}


def analyse_schema(ids):
    # ids as an enum, so the model can't cite "588" or a span name instead of an id
    evidence = {"type": "string", "enum": ids} if ids else {"type": "string"}
    return {
        "type": "object",
        "properties": {"claims": {"type": "array", "maxItems": MAX_CLAIMS, "items": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "enum": CLAIMS},
                "reason": {"type": "string", "maxLength": 200},
                "verdict": {"type": "string", "enum": VERDICTS},
                "certainty": {"type": "number"},
                "evidence_ids": {"type": "array", "maxItems": EVIDENCE_LIMIT, "items": evidence},
            },
            "required": ["claim", "reason", "verdict", "certainty", "evidence_ids"],
        }}},
        "required": ["claims"],
    }


class ModelDown(Exception):
    pass


class BadReply(Exception):
    """The model answered, just not with what we asked for. Asking again won't help."""


def post(url, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chat(system, prompt, schema):
    body = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "format": schema,
        "stream": False,
        "think": False,  # qwen3 thinks by default, too slow for three nodes on one ollama
        "options": {"temperature": 0, "seed": 7, "num_ctx": LLM_NUM_CTX},
    }
    errors = []
    for _ in range(LLM_RETRIES + 1):
        try:
            reply = post(f"{OLLAMA_URL}/api/chat", body, LLM_TIMEOUT)
        except (OSError, http.client.HTTPException, ValueError) as e:
            errors.append(e)
            continue
        try:
            return json.loads(reply["message"]["content"])
        except (ValueError, KeyError, TypeError) as e:
            raise BadReply(f"{OLLAMA_MODEL}: {e!r}")
    raise ModelDown(f"{OLLAMA_MODEL} at {OLLAMA_URL}: {errors[-1]!r}")


def ping():
    """True if Ollama answers and has the model, for /health."""
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=1) as r:
            names = {m["name"] for m in json.load(r)["models"]}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return OLLAMA_MODEL in names or f"{OLLAMA_MODEL}:latest" in names


def _template(message):
    # the same error for another user or ip should land in the same group
    return re.sub(r"[\w.:-]*\d[\w.:-]*", "N", " ".join((message or "").split()))[:MESSAGE_CHARS]


def _groups(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    return ordered[:GROUPS], sum(len(g) for _, g in ordered[GROUPS:])


def _p95(values):
    values = sorted(values)
    return values[int(0.95 * (len(values) - 1))] if values else 0


def _num(x):
    return f"{x:.1f}" if isinstance(x, float) and not x.is_integer() else f"{x:g}"


def _counts(counter):
    return ", ".join(f"{n} {k}" for k, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def summarize(shard):
    """Shard as short text, rows grouped and counted. Returns the text and the ids
    it shows, a few per group. Those are the only ones the model may cite."""
    shown = []

    def ids(rows):
        picked = [r["id"] for r in rows[:SAMPLE_IDS]]
        shown.extend(picked)
        return ", ".join(picked)

    rows = [r for t in shard.values() for r in t]
    times = sorted(str(r["ts"]) for r in rows if r.get("ts"))
    # most columns are nullable, so str() before sorting
    out = [f"window: {times[0][:19]} to {times[-1][:19]}" if times else "window: no rows"]

    logs = shard.get("logs", [])
    severities = f" ({_counts(Counter(str(r.get('severity')) for r in logs))})" if logs else ""
    out.append(f"\nlogs: {len(logs)} rows{severities}")
    groups, rest = _groups(logs, lambda r: (r["severity"], r.get("code") or "-", _template(r["message"])))
    for (severity, code, message), g in groups:
        note = f" ({KNOWN_CODES[code]})" if code in KNOWN_CODES else ""
        out.append(f"- {len(g)} x {severity} code {code}{note}: {message!r} [{ids(g)}]")
    if rest:
        out.append(f"- {rest} more rows in smaller groups")

    traces = shard.get("traces", [])
    out.append(f"\nspans: {len(traces)} rows")
    for target in sorted({str(r.get("target")) for r in traces}):
        status = Counter(str(r.get("status")) for r in traces if str(r.get("target")) == target)
        out.append(f"to {target}: {_counts(status)}")

    def span_key(r):
        attrs = r.get("attrs") if isinstance(r.get("attrs"), dict) else {}  # jsonb, could be anything
        extra = " ".join(f"{k}={attrs[k]}" for k in ("sqlstate", "error", "url") if attrs.get(k))
        if attrs.get("sqlstate") in KNOWN_CODES:
            extra += f" ({KNOWN_CODES[attrs['sqlstate']]})"
        return r["name"], r["target"], r["status"], extra
    groups, rest = _groups(traces, span_key)
    for (name, target, status, extra), g in groups:
        ms = [r.get("duration_ms") or 0 for r in g]
        out.append(f"- {len(g)} x {name!r} to {target} {status}{' ' + extra if extra else ''},"
                   f" p95 {_num(_p95(ms))} ms [{ids(g)}]")
    if rest:
        out.append(f"- {rest} more rows in smaller groups")

    metrics = shard.get("metrics", [])
    out.append(f"\nmetrics: {len(metrics)} rows")
    groups, rest = _groups(metrics, lambda r: (r["metric"], r.get("unit") or ""))
    for (name, unit), g in groups:
        values = [r["value"] for r in g if r.get("value") is not None]
        if not values:
            continue
        # rows come newest first
        out.append(f"- {name} ({unit}) {len(g)} samples: min {_num(min(values))} max {_num(max(values))}"
                   f" newest {_num(values[0])} sum {_num(sum(values))} [{ids(g)}]")
    if rest:
        out.append(f"- {rest} more rows in smaller groups")
    return "\n".join(out), shown


def score(verdict, certainty):
    """Verdict and certainty as one confidence that the claim is true. Never exactly
    0.5, so a "contradicted" can't end up counting as backing a claim."""
    try:
        certainty = min(1.0, max(0.0, float(certainty)))
    except (TypeError, ValueError):
        return None
    if verdict == "supported":
        return round(0.55 + 0.45 * certainty, 3)
    if verdict == "contradicted":
        return round(0.45 - 0.45 * certainty, 3)
    return None  # cannot see


def _say(node, what, reply):
    reason = reply.get("reason") if isinstance(reply, dict) else None
    print(f"{node}: {what}: {reason!r}", file=sys.stderr, flush=True)


class LlmAgent:
    def __init__(self, node):
        self.node = node
        self.error = None  # set when the model failed us, cleared on the next good answer
        self.failed = False  # analyse got no answer, the node tries it again later
        self._summary = None

    def _system(self):
        return SYSTEM.format(node=self.node, sees=SEES.get(self.node, "telemetry of this machine"))

    def summary(self, shard):
        if self._summary is None:
            self._summary = summarize(shard)
        return self._summary

    def analyse(self, shard):
        if not shard or not any(shard.values()):
            return []  # nothing to read, nothing to claim
        text, shown = self.summary(shard)
        prompt = ANALYSE.format(node=self.node, summary=text, limit=EVIDENCE_LIMIT, most=MAX_CLAIMS,
                                claims="\n".join(f"- {c}" for c in CLAIMS))
        try:
            reply = chat(self._system(), prompt, analyse_schema(shown))
        except ModelDown as e:
            self.error, self.failed = str(e), True
            print(f"{self.node}: model down, no claims ({e})", file=sys.stderr, flush=True)
            return []
        except BadReply as e:
            # an abstain, not an error: nothing would ever clear it
            self.error, self.failed = None, False
            print(f"{self.node}: no usable answer, no claims ({e})", file=sys.stderr, flush=True)
            return []
        self.error, self.failed = None, False
        known = set(shown)
        out = []
        items = reply.get("claims") if isinstance(reply, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            claim, verdict, certainty = item.get("claim"), item.get("verdict"), item.get("certainty")
            confidence = score(verdict, certainty)
            evidence = item.get("evidence_ids") if isinstance(item.get("evidence_ids"), list) else []
            evidence = [e for e in evidence if isinstance(e, str) and e in known]
            evidence = list(dict.fromkeys(evidence))[:EVIDENCE_LIMIT]
            _say(self.node, f"{verdict} {certainty} {claim!r}", item)
            if claim not in CLAIMS or claim in {c["text"] for c in out} or not evidence:
                continue
            if verdict != "supported" or confidence is None or confidence < score(verdict, MIN_CERTAINTY):
                continue  # not sure enough to put our name on it
            out.append({"text": claim, "confidence": confidence, "evidence_ids": evidence,
                        "reason": str(item.get("reason", ""))[:300]})
        return out[:MAX_CLAIMS]

    def evaluate(self, claim, shard):
        if not shard or not any(shard.values()):
            return None
        prompt = EVALUATE.format(node=self.node, summary=self.summary(shard)[0], claim=claim)
        try:
            reply = chat(self._system(), prompt, EVALUATE_SCHEMA)
        except ModelDown as e:
            self.error = str(e)
            raise
        except BadReply as e:
            self.error = None
            print(f"{self.node}: no usable answer on {claim!r}, abstaining ({e})", file=sys.stderr, flush=True)
            return None
        self.error = None
        if not isinstance(reply, dict):
            return None
        _say(self.node, f"on {claim!r}: visible {reply.get('visible')}, {reply.get('verdict')}"
                        f" {reply.get('certainty')}", reply)
        if reply.get("visible") is False:
            return None  # a yes/no first is easier for a small model than "cannot see"
        return score(reply.get("verdict"), reply.get("certainty"))
