"""Property checks for belief-state convergence across shuffled replicas."""

from hypothesis import given, strategies as st

from belief import BeliefState, Claim, merge


@st.composite
def update_batches(draw):
    agents = draw(st.lists(st.sampled_from(["agent-a", "agent-b", "agent-c"]), min_size=1, max_size=8))
    evidence = draw(st.lists(st.text(min_size=1, max_size=12), min_size=0, max_size=8))
    confidence = draw(st.lists(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False), min_size=0, max_size=8))
    versions = draw(st.lists(st.integers(min_value=0, max_value=10), min_size=0, max_size=8))
    count = max(len(agents), len(evidence), len(confidence), len(versions), 1)
    return [
        Claim.create(
            "Checkout latency increased",
            evidence_ids=[evidence[index % len(evidence)]] if evidence else [],
            agent_id=agents[index % len(agents)],
            confidence=confidence[index % len(confidence)] if confidence else 0.0,
            version=versions[index % len(versions)] if versions else 0,
        )
        for index in range(count)
    ]


@given(update_batches())
def test_shuffled_updates_converge_to_identical_digest(updates):
    replicas = []
    for update in updates:
        replica = BeliefState()
        replica.add_claim(update)
        replicas.append(replica)

    # Each merge order describes a different delivery schedule across replicas.
    forward = BeliefState()
    reverse = BeliefState()
    for replica in replicas:
        forward = merge(forward, replica)
    for replica in reversed(replicas):
        reverse = merge(replica, reverse)

    assert forward.digest() == reverse.digest()
    assert forward.to_dict() == reverse.to_dict()


def test_claim_hash_ignores_case_and_formatting_and_merge_unions_evidence():
    left = Claim.create("  Database   pool is FULL ", ["log-1"], "agent-a", 0.4)
    right = Claim.create("database pool is full", ["metric-2"], "agent-b", 0.8)

    state = merge(BeliefState(), BeliefState())
    state.add_claim(left)
    state = merge(state, BeliefState({right.claim_id: right}))

    claim = state.claims[left.claim_id]
    assert len(state.claims) == 1
    assert claim.evidence_ids == {"log-1", "metric-2"}
    assert claim.confidence["agent-a"].value == 0.4
    assert claim.confidence["agent-b"].value == 0.8


def test_same_agent_register_uses_version_then_deterministic_value_tiebreak():
    older = Claim.create("Pool exhausted", agent_id="agent-a", confidence=0.9, version=1)
    newer = Claim.create("Pool exhausted", agent_id="agent-a", confidence=0.2, version=2)
    same_version = Claim.create("Pool exhausted", agent_id="agent-a", confidence=0.7, version=2)

    merged = merge(BeliefState({older.claim_id: older}), BeliefState({newer.claim_id: newer}))
    merged = merge(merged, BeliefState({same_version.claim_id: same_version}))

    assert merged.claims[older.claim_id].confidence["agent-a"].version == 2
    assert merged.claims[older.claim_id].confidence["agent-a"].value == 0.7
