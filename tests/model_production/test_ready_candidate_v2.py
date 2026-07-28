from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.model_production.candidate_publisher import (
    CandidateAdmissionError,
    ReadyCandidatePublisher,
)


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def candidate() -> dict[str, object]:
    return {
        "candidate_digest": digest("candidate"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "data_snapshot_digest": digest("data"),
        "feature_schema_digest": digest("features"),
        "thresholds_digest": digest("thresholds"),
        "review_digest": digest("review"),
        "evaluation_digest": digest("evaluation"),
        "runtime_profile_digest": digest("runtime"),
        "rollback_digest": digest("rollback"),
        "author_lineage_digest": digest("author"),
        "symbols": ["600000.SH"],
        "valid_until_ms": 2_000_000,
        "lifecycle_revision": 4,
    }


def test_signed_publication_detects_tampering_expiry_and_bound_identity_drift(
    tmp_path: Path,
) -> None:
    publisher = ReadyCandidatePublisher(
        tmp_path,
        private_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
        publisher_identity="vnpy-admission",
        clock_ms=lambda: 1_000_000,
    )
    record = publisher.publish(candidate())

    assert UUID(record["publication_id"])
    assert publisher.verify(record, expected_bindings=candidate())
    assert not publisher.verify({**record, "symbols": ["000001.SZ"]})
    assert not publisher.verify(record, expected_bindings={**candidate(), "policy_digest": digest("drift")})
    assert not publisher.verify(record, now_ms=record["valid_until_ms"] + 1)


def test_publication_is_atomic_retains_rollback_and_denies_non_vnpy_writer(
    tmp_path: Path,
) -> None:
    publisher = ReadyCandidatePublisher(
        tmp_path,
        private_key=Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32),
        publisher_identity="vnpy-admission",
        clock_ms=lambda: 1_000_000,
    )
    first = publisher.publish(candidate())
    second = publisher.publish({**candidate(), "candidate_digest": digest("candidate-2")})

    assert publisher.read_current()["publication_id"] == second["publication_id"]
    assert publisher.read_rollback()["publication_id"] == first["publication_id"]
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(CandidateAdmissionError, match="CANDIDATE_PUBLISHER_AUTHORITY_DENIED"):
        publisher.publish(candidate(), caller_authority="agent")
