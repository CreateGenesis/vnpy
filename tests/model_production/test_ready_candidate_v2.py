from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vnpy.model_production.candidate_publisher import (
    CandidateAdmissionError,
    HistoricalCandidateGate,
    ReadyCandidatePublisher,
)
from vnpy.model_production.lifecycle import LifecycleStateV3


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


def historical_gate() -> HistoricalCandidateGate:
    return HistoricalCandidateGate(1, "harness", digest("evaluation"), True, ())


def lifecycle() -> LifecycleStateV3:
    value = candidate()
    return LifecycleStateV3(
        candidate_digest=str(value["candidate_digest"]),
        package_digest=str(value["package_digest"]),
        configuration_digest=str(value["configuration_digest"]),
        policy_digest=str(value["policy_digest"]),
        evidence_bundle_digest=digest("evidence-bundle"),
        stage="broker_simulation",
        revision=int(value["lifecycle_revision"]),
        state="ready",
    )


def test_signed_publication_detects_tampering_expiry_and_bound_identity_drift(
    tmp_path: Path,
) -> None:
    publisher = ReadyCandidatePublisher(
        tmp_path,
        private_key=Ed25519PrivateKey.from_private_bytes(bytes(range(32))),
        publisher_identity="vnpy-admission",
        clock_ms=lambda: 1_000_000,
    )
    record = publisher.publish(
        candidate(), historical_gate=historical_gate(), lifecycle_state=lifecycle()
    )

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
    first = publisher.publish(
        candidate(), historical_gate=historical_gate(), lifecycle_state=lifecycle()
    )
    changed = {**candidate(), "candidate_digest": digest("candidate-2")}
    second = publisher.publish(
        changed,
        historical_gate=historical_gate(),
        lifecycle_state=replace(lifecycle(), candidate_digest=digest("candidate-2")),
    )

    assert publisher.read_current()["publication_id"] == second["publication_id"]
    assert publisher.read_rollback()["publication_id"] == first["publication_id"]
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(CandidateAdmissionError, match="CANDIDATE_PUBLISHER_AUTHORITY_DENIED"):
        publisher.publish(
            candidate(),
            historical_gate=historical_gate(),
            lifecycle_state=lifecycle(),
            caller_authority="agent",
        )
