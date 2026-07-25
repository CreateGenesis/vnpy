from __future__ import annotations

import pytest

from vnpy.model_production.lifecycle import LifecycleRequestV2


def request() -> dict:
    return {
        "contract_version": 2, "entity_type": "model_lifecycle_request",
        "request_id": "request-1", "correlation_id": "correlation-1",
        "idempotency_key": "operation-1", "package_digest": "blake3:" + "a" * 64,
        "configuration_digest": "blake3:" + "b" * 64,
        "policy_digest": "blake3:" + "c" * 64,
        "evidence_bundle_digest": "blake3:" + "d" * 64,
        "current_revision": 7, "created_at_ms": 1_000, "expires_at_ms": 2_000,
        "requester_identity": "master", "operation": "enter_stage",
        "current_stage": "shadow", "requested_stage": "gray",
        "payload_digest": "blake3:" + "e" * 64,
    }


def test_python_lifecycle_v2_type_accepts_exact_contract_and_rejects_both_v1_shapes() -> None:
    parsed = LifecycleRequestV2.from_contract(request())
    assert parsed.contract_version == 2
    with pytest.raises(ValueError, match="legacy"):
        LifecycleRequestV2.from_contract({"contract_version": 1, "entity_type": "lifecycle_request"})
    legacy = request()
    legacy["contract_version"] = 1
    legacy["entity_type"] = "model_lifecycle_request"
    with pytest.raises(ValueError, match="legacy"):
        LifecycleRequestV2.from_contract(legacy)
