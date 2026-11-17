"""Model agent checks. Most use a fake model. The ones at the bottom ask the real
one (Ollama, qwen3:8b by default) on rows captured from the platform during a real
surge, split and quiet minute, and only run with LLM_TESTS=1:

    LLM_TESTS=1 .venv/bin/python -m pytest -q tests/test_llm.py
"""
import gzip
import itertools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import llm
import mock
import node as node_module
import verify
from belief import normalize_claim
from node import Node, WrongIncident, make_app

NODES = ("stockholm", "helsinki", "vantaa")
DATA = Path(__file__).parent / "data"
START = datetime(2026, 11, 17, 17, 8, tzinfo=timezone.utc)
END = datetime(2026, 11, 17, 17, 9, tzinfo=timezone.utc)


def shards(scenario):
    with gzip.open(DATA / f"{scenario}.json.gz", "rt") as f:
        return json.load(f)


def ids(shard):
    return [r["id"] for rows in shard.values() for r in rows]


class FakeModel:
    """Stands in for llm.chat: answers from a list, or raises what it's given."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, system, prompt, schema):
        self.prompts.append((system, prompt, schema))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def claim(text, certainty=0.9, evidence=("log:1",), verdict="supported"):
    return {"claim": text, "reason": "because", "verdict": verdict,
            "certainty": certainty, "evidence_ids": list(evidence)}


SHARD = {"traces": [], "metrics": [],
         "logs": [{"id": f"log:{i}", "ts": f"2026-11-17 17:08:0{i}", "severity": "ERROR", "code": "53300",
                   "message": "sorry, too many clients already", "source": "vantaa"} for i in (1, 2, 3, 4)]}


def test_claims_are_normalized_and_shared_with_the_mock():
    for text in llm.CLAIMS:
        assert normalize_claim(text) == text
    assert len(set(llm.CLAIMS)) == len(llm.CLAIMS)
    for text in (mock.DB_REFUSING, mock.SIGNUP_FAILING, mock.PEERS_CUT, mock.DB_HEALTHY):
        assert text in llm.CLAIMS
    # every claim the judge knows about is one the model can pick
    for wrong in verify.WRONG.values():
        assert wrong <= set(llm.CLAIMS)


def test_summary_is_short_and_only_shows_own_ids():
    for scenario in ("surge", "split", "none"):
        for node, shard in shards(scenario).items():
            text, shown = llm.summarize(shard)
            assert len(text) < 6000, (scenario, node)
            assert shown and set(shown) <= set(ids(shard))
            assert all(i in text for i in shown)
            assert llm.summarize(shard) == (text, shown)  # same rows, same prompt


def test_summary_survives_null_columns():
    rows = {"traces": [{"id": "span:a", "name": None, "target": None, "status": None, "duration_ms": None},
                       {"id": "span:b", "name": "x", "target": "userdb", "status": "OK", "duration_ms": 2}],
            "logs": [{"id": "log:1", "ts": None, "severity": None, "code": None, "message": None}],
            "metrics": [{"id": "metric:1", "metric": "m", "unit": None, "value": None},
                        {"id": "metric:2", "metric": None, "unit": "ms", "value": 3}]}
    text, shown = llm.summarize(rows)
    assert "span:b" in shown and "metric:2" in shown and "metric:1" not in shown


def test_summary_keeps_what_tells_faults_apart():
    assert "code 53300" in llm.summarize(shards("surge")["vantaa"])[0]
    assert "OperationalError" in llm.summarize(shards("surge")["stockholm"])[0]
    assert "username already taken" in llm.summarize(shards("surge")["stockholm"])[0]
    split = llm.summarize(shards("split")["helsinki"])[0]
    assert "PEER_UNREACHABLE" in split and "to peer: " in split
    # one group for one error, whatever the username or ip in it
    assert llm._template("login ok for visitor14") == llm._template("login ok for visitor7")
    ip = 'server at "{}", port {}'
    assert llm._template(ip.format("172.28.0.2", 5432)) == llm._template(ip.format("10.0.0.1", 5433))


def test_schema_only_allows_listed_claims_and_shown_ids():
    schema = llm.analyse_schema(["log:1", "log:2"])
    item = schema["properties"]["claims"]["items"]["properties"]
    assert item["claim"]["enum"] == llm.CLAIMS
    assert item["evidence_ids"]["items"]["enum"] == ["log:1", "log:2"]
    assert item["verdict"]["enum"] == llm.VERDICTS


def test_analyse_keeps_only_what_the_model_can_back(monkeypatch):
    monkeypatch.setattr(llm, "chat", FakeModel({"claims": [
        claim(mock.DB_REFUSING, 1.7, ["log:1", "log:1", "log:404", "span:made-up"]),
        claim(mock.DB_REFUSING, 0.6),  # twice
        claim("the database is on fire"),
        claim(mock.DB_HEALTHY, 0.9, ["log:2"], verdict="contradicted"),
        claim(llm.DB_SLOW, 0.9, ["log:2"], verdict="cannot see"),
        claim(mock.SIGNUP_FAILING, 0.3),  # too unsure to put our name on it
        claim(llm.APPS_OK, 0.9, ["log:9"]),  # no real evidence left
        claim(llm.HELSINKI_DOWN, 0.9, ["log:4"]),  # in the shard, but not in what the model was shown
        claim(mock.PEERS_CUT, "high"),
    ]}))
    out = llm.LlmAgent("vantaa").analyse(SHARD)
    kept = [(c["text"], c["confidence"], c["evidence_ids"]) for c in out]
    assert kept == [(mock.DB_REFUSING, 1.0, ["log:1"])]  # certainty 1.7 is just 1


@pytest.mark.parametrize("reply", [None, [], "claims", {"claims": "x"}, {"claims": [1, None]}, {}])
def test_analyse_survives_odd_replies(monkeypatch, reply):
    monkeypatch.setattr(llm, "chat", FakeModel(reply))
    agent = llm.LlmAgent("vantaa")
    assert agent.analyse(SHARD) == []
    assert not agent.failed


def test_analyse_caps_the_number_of_claims(monkeypatch):
    many = [claim(t, 0.9, ["log:1"]) for t in llm.CLAIMS]
    monkeypatch.setattr(llm, "chat", FakeModel({"claims": many}))
    assert len(llm.LlmAgent("vantaa").analyse(SHARD)) == llm.MAX_CLAIMS


def test_no_rows_means_no_model_call(monkeypatch):
    model = FakeModel(AssertionError("asked the model about nothing"))
    monkeypatch.setattr(llm, "chat", model)
    agent = llm.LlmAgent("vantaa")
    empty = {"traces": [], "logs": [], "metrics": []}
    assert agent.analyse(None) == [] and agent.analyse(empty) == []
    assert agent.evaluate(mock.PEERS_CUT, None) is None and agent.evaluate(mock.PEERS_CUT, empty) is None


def test_chat_retries_once_then_gives_up(monkeypatch):
    calls = []

    def post(url, body, timeout):
        calls.append(body)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return {"message": {"content": '{"claims": []}'}}
    monkeypatch.setattr(llm, "post", post)
    assert llm.chat("s", "p", {}) == {"claims": []}
    assert len(calls) == 2 and calls[0]["format"] == {} and calls[0]["options"]["temperature"] == 0

    calls.clear()
    def refused(*a):
        calls.append(1)
        raise ConnectionRefusedError()
    monkeypatch.setattr(llm, "post", refused)
    with pytest.raises(llm.ModelDown):
        llm.chat("s", "p", {})
    assert len(calls) == llm.LLM_RETRIES + 1

    # an answer we can't use is not the model being down, and at temperature 0 asking again
    # gets the same answer
    calls.clear()
    monkeypatch.setattr(llm, "post", lambda *a: calls.append(1) or {"message": {"content": "not json"}})
    with pytest.raises(llm.BadReply):
        llm.chat("s", "p", {})
    assert len(calls) == 1


def test_bad_reply_is_an_abstain_not_a_retry_loop(monkeypatch):
    monkeypatch.setattr(llm, "chat", FakeModel(llm.BadReply("empty")))
    agent = llm.LlmAgent("vantaa")
    assert agent.analyse(SHARD) == [] and not agent.failed
    assert agent.evaluate(mock.PEERS_CUT, SHARD) is None
    # no error left behind: nothing would ever clear it, and verify would wait for it forever
    assert agent.error is None


def test_model_down_means_no_claims_and_an_error(monkeypatch):
    monkeypatch.setattr(llm, "OLLAMA_URL", "http://127.0.0.1:9")
    agent = llm.LlmAgent("vantaa")
    assert agent.analyse(SHARD) == []
    assert agent.failed and "127.0.0.1:9" in agent.error
    with pytest.raises(llm.ModelDown):
        agent.evaluate(mock.PEERS_CUT, SHARD)
    assert llm.ping() is False


@pytest.mark.parametrize("reply,expected", [
    ({"verdict": "supported", "certainty": 1}, 1.0),
    ({"verdict": "supported", "certainty": 0}, 0.55),  # unsure, but still on the yes side
    ({"verdict": "contradicted", "certainty": 0}, 0.45),
    ({"verdict": "contradicted", "certainty": 1}, 0.0),
    ({"verdict": "cannot see", "certainty": 0.9}, None),
    ({"verdict": "supported", "certainty": 3}, 1.0),
    ({"verdict": "supported", "certainty": "high"}, None),
    ({"verdict": "maybe", "certainty": 0.9}, None),
    ({"visible": False, "verdict": "contradicted", "certainty": 1}, None),  # not its business
    ({"visible": True, "verdict": "contradicted", "certainty": 1}, 0.0),
    ([], None),
])
def test_evaluate(monkeypatch, reply, expected):
    monkeypatch.setattr(llm, "chat", FakeModel(reply))
    assert llm.LlmAgent("vantaa").evaluate(mock.PEERS_CUT, SHARD) == expected


def llm_node(name, monkeypatch, shard=SHARD, **kw):
    n = Node(name, mode="llm", **kw)
    monkeypatch.setattr(n, "read_shard", lambda start, end: shard)
    return n


def test_cross_check_asks_again_after_the_model_was_down(monkeypatch):
    vantaa = llm_node("vantaa", monkeypatch)
    model = FakeModel({"claims": []})
    monkeypatch.setattr(llm, "chat", model)
    vantaa.start_incident("inc-1", START, END)
    other = Node("stockholm")
    other.start_incident("inc-1", START, END, "surge")
    vantaa.exchange("inc-1", "stockholm", other.state.to_dict())

    model.answers = [llm.ModelDown("down")]
    assert vantaa.cross_check() is False
    assert vantaa.snapshot()["pending"] == 1 and vantaa.snapshot()["error"] == "down"

    model.answers = [{"verdict": "supported", "certainty": 0.5}]
    assert vantaa.cross_check() is True
    signup = next(c for c in vantaa.state.claims.values() if c.text == mock.SIGNUP_FAILING)
    assert signup.confidence["vantaa"].value == 0.775
    assert vantaa.snapshot()["pending"] == 0 and vantaa.snapshot()["error"] is None


def test_own_claims_come_back_once_the_model_does(monkeypatch):
    vantaa = llm_node("vantaa", monkeypatch)
    model = FakeModel(llm.ModelDown("down"))
    monkeypatch.setattr(llm, "chat", model)
    r = TestClient(make_app(vantaa)).post("/incident", json={"id": "inc-1"}).json()
    assert r["claims"] == [] and "down" in r["error"]
    assert vantaa.reanalyse() is False and not vantaa.state.claims

    model.answers = [{"claims": [claim(mock.DB_REFUSING)]}]
    assert vantaa.reanalyse() is True
    assert [c.text for c in vantaa.state.claims.values()] == [mock.DB_REFUSING]
    assert vantaa.reanalyse() is True and len(model.prompts) == 3  # nothing left to redo


def test_pending_stays_right_when_a_new_incident_comes_mid_check(monkeypatch):
    vantaa = llm_node("vantaa", monkeypatch)
    stockholm = Node("stockholm")
    inside, gate = threading.Event(), threading.Event()

    def chat(system, prompt, schema):
        if "Another agent claims" in prompt:
            inside.set()
            gate.wait(5)
            return {"verdict": "supported", "certainty": 0.5}
        return {"claims": []}
    monkeypatch.setattr(llm, "chat", chat)
    checks = []
    for incident in ("inc-1", "inc-2"):  # inc-2 starts while the inc-1 check still runs
        vantaa.start_incident(incident, START, END)
        stockholm.start_incident(incident, START, END, "surge")
        vantaa.exchange(incident, "stockholm", stockholm.state.to_dict())
        inside.clear()
        checks.append(threading.Thread(target=vantaa.cross_check))
        checks[-1].start()
        assert inside.wait(5)
        assert vantaa.snapshot()["pending"] == 1  # the one the model is on, not the old one too
    gate.set()
    for check in checks:
        check.join()
    assert vantaa.snapshot()["pending"] == 0
    assert vantaa.busy["inc-1"] == vantaa.busy["inc-2"] == 0


def test_incident_and_join_never_analyse_at_the_same_time(monkeypatch):
    old = Node("stockholm")
    old.start_incident("old", START, END, "surge")
    body = {"incident": "old", "from": "stockholm", "state": old.state.to_dict(), "window": old.window}
    vantaa = llm_node("vantaa", monkeypatch)
    client = TestClient(make_app(vantaa))
    started, release = threading.Event(), threading.Event()
    calls = []

    def chat(system, prompt, schema):
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(5)
        return {"claims": [claim(mock.DB_REFUSING)]}
    monkeypatch.setattr(llm, "chat", chat)

    # /incident is thinking, a fresh node gets gossip for another incident meanwhile
    new = threading.Thread(target=vantaa.start_incident, args=("new", START, END), name="incident")
    new.start()
    assert started.wait(5)
    assert client.post("/exchange", json=body).status_code == 409
    release.set()
    new.join()
    assert vantaa.incident == "new" and calls == ["incident"]

    # the other way round: a join is thinking when /incident comes, the alert wins
    fresh = llm_node("helsinki", monkeypatch)
    started.clear()
    release.clear()
    replies = []
    join = threading.Thread(target=lambda: replies.append(TestClient(make_app(fresh)).post(
        "/exchange", json=body).status_code), name="join")
    join.start()
    assert started.wait(5)
    alert = threading.Thread(target=fresh.start_incident, args=("new", START, END), name="alert")
    alert.start()
    release.set()
    join.join()
    alert.join()
    # the join's own merge can come before or after the alert switches incident
    assert fresh.incident == "new" and replies in ([200], [409])


def test_node_owes_work_until_its_retried_claims_are_in(monkeypatch):
    vantaa = llm_node("vantaa", monkeypatch)
    monkeypatch.setattr(llm, "chat", FakeModel(llm.ModelDown("down")))
    vantaa.start_incident("inc-1", START, END)
    assert vantaa.snapshot()["pending"]  # its own analysis failed
    inside, gate = threading.Event(), threading.Event()

    def chat(system, prompt, schema):
        inside.set()
        gate.wait(5)
        return {"claims": [claim(mock.DB_REFUSING)]}
    monkeypatch.setattr(llm, "chat", chat)
    retry = threading.Thread(target=vantaa.reanalyse)
    retry.start()
    assert inside.wait(5)
    assert vantaa.snapshot()["pending"]
    gate.set()
    retry.join()
    assert vantaa.snapshot()["pending"] == 0 and len(vantaa.state.claims) == 1


def test_old_check_stops_asking_once_the_incident_moved_on(monkeypatch):
    vantaa = llm_node("vantaa", monkeypatch)
    stockholm = Node("stockholm")
    monkeypatch.setattr(llm, "chat", FakeModel({"claims": []}))
    vantaa.start_incident("inc-1", START, END)
    stockholm.start_incident("inc-1", START, END, "surge")
    helsinki = Node("helsinki")
    helsinki.start_incident("inc-1", START, END, "split")
    vantaa.exchange("inc-1", "stockholm", stockholm.state.merge(helsinki.state).to_dict())
    assert vantaa.snapshot()["pending"] == 2
    asked = []

    def chat(system, prompt, schema):
        asked.append(prompt)
        vantaa.incident = "inc-2"  # a new alert lands while the model answers the first one
        return {"visible": True, "verdict": "supported", "certainty": 1}
    monkeypatch.setattr(llm, "chat", chat)
    vantaa.cross_check()
    assert len(asked) == 1


def test_health_says_if_the_model_is_there(monkeypatch):
    monkeypatch.setattr(llm, "ping", lambda: False)
    h = TestClient(make_app(Node("vantaa", mode="llm"))).get("/health").json()
    assert h["mode"] == "llm" and h["model"] is False and h["ok"] is True
    monkeypatch.setattr(llm, "ping", lambda: True)
    assert TestClient(make_app(Node("vantaa", mode="llm"))).get("/health").json()["model"] is True


def test_ping_wants_the_model_not_just_ollama(monkeypatch):
    class Reply:
        def __init__(self, names):
            self.body = json.dumps({"models": [{"name": n} for n in names]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, *a):
            return self.body
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: Reply(["qwen2.5:0.5b"]))
    assert llm.ping() is False
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: Reply([llm.OLLAMA_MODEL]))
    assert llm.ping() is True


def test_second_join_while_the_model_thinks_gets_a_409(monkeypatch):
    stockholm = Node("stockholm")
    stockholm.start_incident("inc-1", START, END, "surge")
    vantaa = llm_node("vantaa", monkeypatch)
    started, release = threading.Event(), threading.Event()

    def slow(system, prompt, schema):
        started.set()
        release.wait(5)
        return {"claims": [claim(mock.DB_REFUSING)]}
    monkeypatch.setattr(llm, "chat", slow)
    client = TestClient(make_app(vantaa))
    body = {"incident": "inc-1", "from": "stockholm", "state": stockholm.state.to_dict(),
            "window": stockholm.window}
    first = threading.Thread(target=lambda: client.post("/exchange", json=body))
    first.start()
    assert started.wait(5)
    t0 = time.perf_counter()
    assert client.post("/exchange", json=body).status_code == 409
    assert time.perf_counter() - t0 < 1
    release.set()
    first.join()
    assert vantaa.incident == "inc-1"
    assert client.post("/exchange", json=body).status_code == 200
    with pytest.raises(WrongIncident):
        vantaa.exchange("inc-2", "stockholm", {})


def test_hanging_model_does_not_stop_gossip(monkeypatch):
    stockholm = Node("stockholm")
    stockholm.start_incident("inc-1", START, END, "surge")
    vantaa = llm_node("vantaa", monkeypatch, peers={"stockholm": "http://stockholm"})
    monkeypatch.setattr(llm, "chat", FakeModel({"claims": []}))
    vantaa.start_incident("inc-1", START, END)
    vantaa.exchange("inc-1", "stockholm", stockholm.state.to_dict())
    hang = threading.Event()
    monkeypatch.setattr(llm, "chat", lambda *a: hang.wait(5) and {})
    threading.Thread(target=vantaa.cross_check, daemon=True).start()

    sent = []
    monkeypatch.setattr(node_module, "post", lambda url, body, timeout: sent.append(body) or
                        {"incident": "inc-1", "state": stockholm.state.to_dict()})
    t0 = time.perf_counter()
    vantaa.gossip_once()
    assert sent and time.perf_counter() - t0 < 1
    assert vantaa.snapshot()["pending"] == 1  # the one being thought about
    hang.set()


def test_settled():
    def s(node, digest="d", pending=0, error=None, peers=None):
        return {"node": node, "incident": "i", "digest": digest, "pending": pending, "error": error,
                "peers": dict.fromkeys(peers if peers is not None else set(NODES) - {node}, 1.0)}
    ok = [s(n) for n in NODES]
    assert verify.settled(ok)
    assert not verify.settled([s("stockholm", digest="x")] + ok[1:])
    assert not verify.settled([s("stockholm", peers={"vantaa"})] + ok[1:])
    assert not verify.settled([s("stockholm", pending=2)] + ok[1:])
    # a model error is not an excuse to stop waiting, unless we took the model away on purpose
    assert not verify.settled([s("stockholm", pending=2, error="down")] + ok[1:])
    assert not verify.settled([s("stockholm", error="bad reply")] + ok[1:])
    assert verify.settled([s("stockholm", pending=2, error="down")] + ok[1:], down={"stockholm"})
    pair = [s("stockholm", peers={"helsinki"}), s("helsinki", peers={"stockholm"})]
    assert verify.settled(pair, {"stockholm", "helsinki"})


def test_judge():
    def c(text, **votes):
        return {"text": text, "evidence_ids": ["log:1"],
                "confidence": {n: {"version": 1, "value": v} for n, v in votes.items()}}
    good = [c(mock.DB_REFUSING, vantaa=0.9, stockholm=0.8),
            c(mock.SIGNUP_FAILING, stockholm=0.9, helsinki=0.8, vantaa=0.4)]
    assert verify.judge("surge", good) == []
    assert verify.judge("surge", [c(mock.DB_REFUSING, stockholm=0.9), good[1]])  # vantaa never backed it
    assert verify.judge("surge", good[:1])  # one machine alone is not a diagnosis
    assert verify.judge("surge", good[:1], down={"stockholm", "helsinki"}) == []
    assert verify.judge("surge", good + [c(llm.DB_SLOW, helsinki=0.6)])
    assert verify.judge("surge", good + [c(llm.DB_SLOW, helsinki=0.6, stockholm=0.0)]) == []
    assert verify.judge("split", good)
    assert verify.judge("none", [])  # saying nothing is not a quiet day either
    assert verify.judge("none", [c(mock.DB_HEALTHY, vantaa=0.9)]) == []
    fake = dict(good[0], evidence_ids=["mock:vantaa-1"])
    assert verify.judge("surge", [fake, good[1]])


# from here on the real model, a few minutes per scenario

live = pytest.mark.skipif(os.environ.get("LLM_TESTS") != "1", reason="real model, set LLM_TESTS=1")


def settle(nodes):
    for _ in range(10):
        for a, b in itertools.combinations(nodes, 2):
            reply = b.exchange(a.incident, a.id, a.state.to_dict(), a.window)
            a.exchange(b.incident, b.id, reply)
        for n in nodes:
            assert n.cross_check(), f"{n.id}: {n.snapshot()['error']}"
        states = [n.snapshot() for n in nodes]
        if len({s["digest"] for s in states}) == 1 and not any(s["pending"] for s in states):
            return states[0]["state"]
    raise AssertionError("no convergence after 10 rounds")


@live
@pytest.mark.parametrize("scenario,rows", list(itertools.product(["surge", "split", "none"], ["all", "half"])))
def test_real_model_finds_the_fault(monkeypatch, scenario, rows):
    assert llm.ping(), f"{llm.OLLAMA_MODEL} not on {llm.OLLAMA_URL}, ollama pull it first"
    data = shards(scenario)
    if rows == "half":  # other numbers and other ids, same fault
        data = {n: {t: r[1::2] for t, r in s.items()} for n, s in data.items()}
    nodes = [llm_node(n, monkeypatch, data[n]) for n in NODES]
    threads = [threading.Thread(target=n.start_incident, args=("inc-1", START, END)) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claims = list(settle(nodes).values())
    verify.show(scenario, claims)
    assert verify.judge(scenario, claims) == []


@live
def test_real_model_comes_back_after_being_down(monkeypatch):
    assert llm.ping()
    url = llm.OLLAMA_URL
    vantaa = llm_node("vantaa", monkeypatch, shards("surge")["vantaa"])
    monkeypatch.setattr(llm, "OLLAMA_URL", "http://127.0.0.1:9")
    assert vantaa.start_incident("inc-1", START, END)["error"]
    assert not vantaa.state.claims
    monkeypatch.setattr(llm, "OLLAMA_URL", url)
    assert vantaa.reanalyse()
    assert mock.DB_REFUSING in {c.text for c in vantaa.state.claims.values()}
