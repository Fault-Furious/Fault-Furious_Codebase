"""Node and mock agent checks. No Docker, no DB: nodes run without a DSN."""
import itertools
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings, strategies as st

import mock
import node as node_module
from belief import Claim, normalize_claim
from node import Node, make_app

NODES = ("stockholm", "helsinki", "vantaa")
START = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 2, 9, 1, tzinfo=timezone.utc)
EXPECTED = {
    "surge": {"stockholm": [mock.SIGNUP_FAILING], "helsinki": [mock.SIGNUP_FAILING], "vantaa": [mock.DB_REFUSING]},
    "split": {"stockholm": [mock.PEERS_CUT], "helsinki": [mock.PEERS_CUT], "vantaa": [mock.DB_HEALTHY]},
    "none": {"stockholm": [], "helsinki": [], "vantaa": [mock.DB_HEALTHY]},
}
# who ends up with a register on each claim, vantaa abstains on the link between the apps
VOTERS = {
    mock.SIGNUP_FAILING: set(NODES),
    mock.DB_REFUSING: set(NODES),
    mock.DB_HEALTHY: set(NODES),
    mock.PEERS_CUT: {"stockholm", "helsinki"},
}


def started(scenario, incident="inc-1"):
    nodes = [Node(n) for n in NODES]
    for n in nodes:
        n.start_incident(incident, START, END, scenario)
    return nodes


def swap(a, b):
    reply = b.exchange(a.incident, a.id, a.state.to_dict(), a.window)
    a.exchange(b.incident, b.id, reply)
    a.cross_check()
    b.cross_check()


def gossip_until_equal(nodes, order):
    for _ in range(5):
        for i, j in order:
            swap(nodes[i], nodes[j])
        if len({n.state.digest() for n in nodes}) == 1:
            return
    raise AssertionError("no convergence after 5 rounds")


def summary(node):
    """State without versions, which come from the clock and differ per run."""
    out = {}
    for c in node.state.claims.values():
        out[c.text] = {agent: r.value for agent, r in c.confidence.items()}
        out[c.text]["evidence"] = sorted(c.evidence_ids)
    return out


def mean(claim):
    return sum(r.value for r in claim.confidence.values()) / len(claim.confidence)


def shard(traces=(), logs=(), metrics=()):
    return {"traces": list(traces), "logs": list(logs), "metrics": list(metrics)}


def test_claim_texts_are_already_normalized():
    # belief.py hashes the normalized text, cross-check looks claims up by it
    for text in VOTERS:
        assert normalize_claim(text) == text


def test_health():
    r = TestClient(make_app(Node("vantaa"))).get("/health")
    assert r.status_code == 200
    assert r.json() == {"node": "vantaa", "mode": "mock", "ok": True, "incident": None, "db": None}


@pytest.mark.parametrize("scenario,node", list(itertools.product(EXPECTED, NODES)))
def test_incident_returns_hardcoded_claims(scenario, node):
    client = TestClient(make_app(Node(node)))
    r = client.post("/incident", json={"id": "inc-1", "scenario": scenario})
    assert r.status_code == 200
    body = r.json()
    assert body["node"] == node and body["incident"] == "inc-1" and body["scenario"] == scenario
    assert [c["text"] for c in body["claims"]] == EXPECTED[scenario][node]
    assert body["records"] is None and body["sources"] is None
    assert all(c["evidence_ids"] == [f"mock:{node}-1"] for c in body["claims"])


def test_without_scenario_or_db_it_is_a_quiet_day():
    r = TestClient(make_app(Node("vantaa"))).post("/incident", json={"id": "inc-1"})
    assert r.json()["scenario"] == "none"
    assert [c["text"] for c in r.json()["claims"]] == [mock.DB_HEALTHY]


def test_mock_detects_the_fault_from_its_own_rows():
    db_error = {"id": "span:a", "target": "userdb", "status": "ERROR", "attrs": {}}
    taken = {"id": "span:t", "target": "userdb", "status": "ERROR", "attrs": {"sqlstate": "23505"}}
    peer = {"id": "log:2", "severity": "WARN", "code": "PEER_UNREACHABLE"}
    refused = {"id": "log:9", "severity": "FATAL", "code": "53300"}

    assert mock.detect("stockholm", shard(traces=[db_error], logs=[peer])) == "surge"
    assert mock.detect("helsinki", shard(logs=[peer])) == "split"
    assert mock.detect("helsinki", shard(traces=[taken])) == "none"  # username taken is not a fault
    assert mock.detect("vantaa", shard(logs=[refused])) == "surge"
    assert mock.detect("vantaa", shard()) == "none"

    agent = mock.MockAgent("helsinki")
    assert [h["text"] for h in agent.analyse(shard(logs=[peer]))] == [mock.PEERS_CUT]
    assert agent.scenario == "split"


def test_exchange_merges_and_answers_with_local_state():
    vantaa = Node("vantaa")
    vantaa.start_incident("inc-1", START, END, "surge")
    stockholm = Node("stockholm")
    stockholm.start_incident("inc-1", START, END, "surge")

    client = TestClient(make_app(vantaa))
    r = client.post("/exchange", json={"incident": "inc-1", "from": "stockholm", "state": stockholm.state.to_dict()})
    assert r.status_code == 200
    assert {c["text"] for c in r.json()["state"].values()} == {mock.DB_REFUSING, mock.SIGNUP_FAILING}
    assert "stockholm" in client.get("/state").json()["peers"]

    # the opinion comes from the gossip loop, not from inside the request
    vantaa.cross_check()
    signup = next(c for c in vantaa.state.claims.values() if c.text == mock.SIGNUP_FAILING)
    assert signup.confidence["vantaa"].value == 0.5


def test_exchange_rejects_other_incident_and_bad_state():
    node = Node("helsinki")
    node.start_incident("inc-1", START, END, "split")
    client = TestClient(make_app(node))
    assert client.post("/exchange", json={"incident": "inc-2", "from": "x", "state": {}}).status_code == 409
    bad = {"abc": {"text": "something", "evidence_ids": []}}  # key is not the claim hash
    assert client.post("/exchange", json={"incident": "inc-1", "from": "x", "state": bad}).status_code == 400
    assert client.post("/exchange", json={"incident": "inc-1", "from": "x", "state": {"a": "b"}}).status_code == 400
    assert client.post("/exchange", json={"incident": "inc-1"}).status_code == 422


def test_bad_exchange_leaves_state_alone():
    node = Node("stockholm")
    node.start_incident("inc-1", START, END, "surge")
    before = node.state.digest()
    client = TestClient(make_app(node), raise_server_exceptions=False)
    claim = Claim.create(mock.SIGNUP_FAILING)
    for state in (
        {claim.claim_id: {"text": claim.text, "evidence_ids": [1]}},  # would break sorting later
        {claim.claim_id: {"text": claim.text, "evidence_ids": "log:12"}},  # would become characters
        {claim.claim_id: {"text": claim.text, "confidence": []}},
        {"claims": [1]},
    ):
        r = client.post("/exchange", json={"incident": "inc-1", "from": "x", "state": state})
        assert r.status_code == 400, state
    assert client.get("/state").status_code == 200
    assert node.state.digest() == before


def test_gossip_survives_garbage_from_peer(monkeypatch):
    node = Node("stockholm", peers={"helsinki": "http://helsinki"})
    node.start_incident("inc-1", START, END, "split")
    for reply in ({"incident": "inc-1", "state": {"k": "str"}}, [], {"incident": "inc-1", "state": {"claims": [1]}}):
        monkeypatch.setattr(node_module, "post", lambda *a, reply=reply: reply)
        node.gossip_once()
        assert node.unreachable == {"helsinki"}


def test_incident_input_is_validated():
    client = TestClient(make_app(Node("vantaa")))
    assert client.post("/incident", json={"id": "i", "scenario": "Surge"}).status_code == 422
    window = {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T09:00:00Z"}
    assert client.post("/incident", json={"id": "i"} | window).status_code == 422
    # no zone means UTC, not a crash comparing naive and aware times
    assert client.post("/incident", json={"id": "i", "start": "2026-11-02T09:00:00",
                                          "end": "2026-11-02T09:01:00"}).status_code == 200


def test_node_refuses_rows_of_other_machines(monkeypatch):
    rows = shard(traces=[{"source": "helsinki"}])
    monkeypatch.setattr(node_module, "get_shard", lambda *a: rows)
    client = TestClient(make_app(Node("stockholm", dsn="postgresql://admin@x/telemetry")),
                        raise_server_exceptions=False)
    r = client.post("/incident", json={"id": "i", "scenario": "surge"})
    assert r.status_code == 500 and "helsinki" in r.json()["detail"]


def test_node_without_id_does_not_start():
    with pytest.raises(RuntimeError):
        with TestClient(make_app(Node(""))):
            pass


def test_new_incident_forgets_peers():
    node = Node("vantaa")
    node.start_incident("inc-1", START, END, "surge")
    node.exchange("inc-1", "stockholm", {})
    assert set(node.last_seen) == {"stockholm"}
    node.start_incident("inc-2", START, END, "surge")
    assert node.last_seen == {}


def test_node_without_incident_refuses_gossip_without_window():
    client = TestClient(make_app(Node("vantaa")))
    assert client.post("/exchange", json={"incident": "inc-1", "from": "x", "state": {}}).status_code == 409


def test_restarted_node_joins_from_gossip():
    stockholm, helsinki, vantaa = started("surge")
    swap(stockholm, helsinki)
    fresh = Node("vantaa")  # vantaa came back with nothing

    client = TestClient(make_app(fresh))
    r = client.post("/exchange", json={"incident": "inc-1", "from": "stockholm",
                                       "state": stockholm.state.to_dict(), "window": stockholm.window})
    assert r.status_code == 200
    assert fresh.incident == "inc-1"
    assert mock.DB_REFUSING in {c["text"] for c in r.json()["state"].values()}

    nodes = [stockholm, helsinki, fresh]
    gossip_until_equal(nodes, list(itertools.combinations(range(3), 2)))
    assert summary(fresh) == summary(stockholm)


def test_join_with_bad_window_is_refused():
    client = TestClient(make_app(Node("vantaa")))
    for window in ({"start": "yesterday", "end": "today"}, {"start": START.isoformat()},
                   {"start": START.isoformat(), "end": END.isoformat(), "scenario": "boom"}):
        r = client.post("/exchange", json={"incident": "inc-1", "from": "x", "state": {}, "window": window})
        assert r.status_code == 400, window


def test_slow_evaluate_does_not_block_the_node():
    node = Node("vantaa")
    node.start_incident("inc-1", START, END, "surge")
    other = Node("stockholm")
    other.start_incident("inc-1", START, END, "surge")
    node.exchange("inc-1", "stockholm", other.state.to_dict())

    def slow(claim, shard):
        time.sleep(1)
        return 0.5
    node.agent.evaluate = slow

    worker = threading.Thread(target=node.cross_check)
    worker.start()
    time.sleep(0.1)
    t0 = time.perf_counter()
    node.snapshot()
    node.exchange("inc-1", "stockholm", other.state.to_dict())
    assert time.perf_counter() - t0 < 0.5  # neither waited for the model
    worker.join()
    signup = next(c for c in node.state.claims.values() if c.text == mock.SIGNUP_FAILING)
    assert signup.confidence["vantaa"].value == 0.5


def test_each_claim_is_scored_once():
    node = Node("vantaa")
    node.start_incident("inc-1", START, END, "split")
    other = Node("stockholm")
    other.start_incident("inc-1", START, END, "split")
    calls = []
    node.agent.evaluate = lambda claim, shard: calls.append(claim)  # abstains every time
    for _ in range(3):
        node.exchange("inc-1", "stockholm", other.state.to_dict())
        node.cross_check()
    assert calls == [mock.PEERS_CUT]


def test_new_incident_resets_state():
    node = Node("vantaa")
    node.start_incident("inc-1", START, END, "surge")
    node.start_incident("inc-2", START, END, "split")
    assert [c.text for c in node.state.claims.values()] == [mock.DB_HEALTHY]


def test_same_incident_again_keeps_state_and_bumps_version():
    node = Node("vantaa")
    node.start_incident("inc-1", START, END, "surge")
    first = next(iter(node.state.claims.values())).confidence["vantaa"].version
    node.start_incident("inc-1", START, END, "surge")
    assert len(node.state.claims) == 1
    assert next(iter(node.state.claims.values())).confidence["vantaa"].version > first


PAIRS = list(itertools.combinations(range(3), 2))


@settings(max_examples=30, deadline=None)
@given(st.sampled_from(list(EXPECTED)), st.permutations(PAIRS))
def test_three_nodes_converge_in_any_order(scenario, order):
    nodes = started(scenario)
    gossip_until_equal(nodes, order)
    claims = nodes[0].state.claims.values()
    assert {c.text for c in claims} == set(sum(EXPECTED[scenario].values(), []))
    for claim in claims:
        assert set(claim.confidence) == VOTERS[claim.text]


def test_converged_content_does_not_depend_on_order():
    results = []
    for order in itertools.permutations(PAIRS):
        nodes = started("surge")
        gossip_until_equal(nodes, order)
        results.append(summary(nodes[0]))
    assert all(r == results[0] for r in results)
    assert results[0][mock.SIGNUP_FAILING]["evidence"] == ["mock:helsinki-1", "mock:stockholm-1"]
    assert results[0][mock.DB_REFUSING] == {"vantaa": 0.9, "stockholm": 0.8, "helsinki": 0.8,
                                             "evidence": ["mock:vantaa-1"]}


def test_cause_ends_up_above_symptom():
    nodes = started("surge")
    gossip_until_equal(nodes, PAIRS)
    claims = {c.text: c for c in nodes[0].state.claims.values()}
    assert mean(claims[mock.DB_REFUSING]) > mean(claims[mock.SIGNUP_FAILING])


def test_gossip_to_dead_peer_does_not_crash():
    node = Node("stockholm", peers={"helsinki": "http://127.0.0.1:9"})
    node.start_incident("inc-1", START, END, "split")
    before = node.state.digest()
    node.gossip_once()
    assert node.last_seen == {}
    assert node.unreachable == {"helsinki"}
    assert node.state.digest() == before


def test_mock_picks_real_evidence_from_shard():
    rows = shard(
        traces=[{"id": "span:a", "target": "userdb", "status": "ERROR", "attrs": {}},
                {"id": "span:b", "target": "http", "status": "OK", "attrs": {}},
                {"id": "span:c", "target": "userdb", "status": "ERROR", "attrs": {"sqlstate": "23505"}}],
        logs=[{"id": "log:1", "severity": "ERROR", "code": None},
              {"id": "log:2", "severity": "WARN", "code": "PEER_UNREACHABLE"},
              {"id": "log:3", "severity": "ERROR", "code": "23505"}],
    )
    surge = mock.MockAgent("stockholm", "surge").analyse(rows)
    assert surge[0]["evidence_ids"] == ["span:a", "log:1"]
    split = mock.MockAgent("helsinki", "split").analyse(rows)
    assert split[0]["evidence_ids"] == ["log:2"]

    db = shard(metrics=[{"id": "metric:7", "metric": "db.connections.refused", "value": 3}],
               logs=[{"id": "log:9", "severity": "ERROR", "code": "53300"}])
    assert mock.MockAgent("vantaa", "surge").analyse(db)[0]["evidence_ids"] == ["log:9", "metric:7"]
