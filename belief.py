"""Convergent belief state for exchanging incident claims between agents.

Claims are identified by the SHA-256 hash of their normalized text. Evidence
is a grow-only set. Confidence is a last-writer-wins register per agent; each
register uses a monotonically increasing version, with the confidence value as
a deterministic tie-breaker when versions match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping


def normalize_claim(text: str) -> str:
    """Normalize formatting and casing before hashing a claim."""
    if not isinstance(text, str):
        raise TypeError("claim text must be a string")
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", normalized).strip().casefold()


def claim_hash(text: str) -> str:
    """Return a stable content hash for a claim."""
    normalized = normalize_claim(text)
    if not normalized:
        raise ValueError("claim text must not be empty")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, order=True)
class ConfidenceRegister:
    """A per-agent LWW register; larger (version, value) wins."""

    version: int
    value: float

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("confidence version must be a non-negative integer")
        if not isinstance(self.version, int):
            raise TypeError("confidence version must be an integer")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise TypeError("confidence value must be numeric")
        if not 0.0 <= float(self.value) <= 1.0:
            raise ValueError("confidence value must be between 0 and 1")
        object.__setattr__(self, "value", float(self.value))


@dataclass
class Claim:
    """One canonical claim and the evidence/confidence accumulated for it."""

    claim_id: str
    text: str
    evidence_ids: set[str] = field(default_factory=set)
    confidence: dict[str, ConfidenceRegister] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        text: str,
        evidence_ids: set[str] | list[str] | tuple[str, ...] = (),
        agent_id: str | None = None,
        confidence: float | None = None,
        version: int = 0,
    ) -> "Claim":
        canonical_text = normalize_claim(text)
        identifier = claim_hash(canonical_text)
        claim = cls(identifier, canonical_text, set(evidence_ids))
        if agent_id is not None or confidence is not None:
            if not agent_id or confidence is None:
                raise ValueError("agent_id and confidence must be provided together")
            claim.confidence[agent_id] = ConfidenceRegister(version, confidence)
        return claim

    def merge(self, other: "Claim") -> "Claim":
        if self.claim_id != other.claim_id:
            raise ValueError("cannot merge claims with different content hashes")
        evidence = self.evidence_ids | other.evidence_ids
        confidence = dict(self.confidence)
        for agent_id, incoming in other.confidence.items():
            current = confidence.get(agent_id)
            if current is None or incoming > current:
                confidence[agent_id] = incoming
        return Claim(self.claim_id, self.text, evidence, confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "evidence_ids": sorted(self.evidence_ids),
            "confidence": {
                agent_id: {"version": register.version, "value": register.value}
                for agent_id, register in sorted(self.confidence.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Claim":
        text = normalize_claim(data["text"])
        identifier = data.get("claim_id", claim_hash(text))
        if identifier != claim_hash(text):
            raise ValueError("claim_id does not match the normalized claim text")
        confidence = {
            agent_id: ConfidenceRegister(int(register["version"]), register["value"])
            for agent_id, register in data.get("confidence", {}).items()
        }
        return cls(identifier, text, set(data.get("evidence_ids", [])), confidence)


@dataclass
class BeliefState:
    """A replica's set of claims with an associative, commutative merge."""

    claims: dict[str, Claim] = field(default_factory=dict)

    def add_claim(self, claim: Claim) -> None:
        existing = self.claims.get(claim.claim_id)
        self.claims[claim.claim_id] = claim if existing is None else existing.merge(claim)

    def merge(self, other: "BeliefState") -> "BeliefState":
        result = BeliefState({key: value for key, value in self.claims.items()})
        for claim in other.claims.values():
            result.add_claim(claim)
        return result

    def digest(self) -> str:
        """Hash a canonical serialization, independent of insertion order."""
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {key: self.claims[key].to_dict() for key in sorted(self.claims)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BeliefState":
        raw_claims = data.get("claims", data)
        claims: dict[str, Claim] = {}
        for key, raw in raw_claims.items():
            claim = Claim.from_dict(raw)
            if key != claim.claim_id:
                raise ValueError("claim map key does not match claim_id")
            claims[key] = claim
        return cls(claims)


def merge(local: BeliefState | Mapping[str, Any], remote: BeliefState | Mapping[str, Any]) -> BeliefState:
    """Merge two states and return a new state without mutating either input."""
    left = local if isinstance(local, BeliefState) else BeliefState.from_dict(local)
    right = remote if isinstance(remote, BeliefState) else BeliefState.from_dict(remote)
    return left.merge(right)

