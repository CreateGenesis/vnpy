from __future__ import annotations

from dataclasses import replace

import pytest

from vnpy.model_production.lifecycle import (
    BrokerSimulationAdmissionEvidence,
    LifecycleAuthority,
    LifecycleAuthorityV3,
    LifecycleRequest,
    LifecycleStateV3,
    valid_transition_v3,
)


def digest(character: str) -> str:
    return "blake3:" + character * 64


def contract() -> dict[str, object]:
    return {
        "contract_version": 3,
        "entity_type": "model_lifecycle",
        "candidate_digest": digest("a"),
        "package_digest": digest("b"),
        "configuration_digest": digest("c"),
        "policy_digest": digest("d"),
        "evidence_bundle_digest": digest("e"),
        "stage": "broker_simulation",
        "revision": 8,
        "state": "ready",
        "gateway_binding_digest": digest("f"),
        "campaign_id": "3bcf350e-d033-45cd-93a5-68812553e3c5",
    }


def admission() -> BrokerSimulationAdmissionEvidence:
    return BrokerSimulationAdmissionEvidence(
        candidate_digest=digest("a"),
        package_digest=digest("b"),
        configuration_digest=digest("c"),
        policy_digest=digest("d"),
        evidence_bundle_digest=digest("e"),
        metric_owner="harness",
        candidate_gate_passed=True,
        rqdata_tick_ready=True,
        observer_converged=True,
        runtime_ready=True,
        review_ready=True,
        hard_risk_ready=True,
        reconciliation_ready=True,
        gateway_bindings_ready=True,
        unresolved_outcomes=0,
        hard_breaker_active=False,
    )


def test_lifecycle_v3_accepts_exact_contract_and_rejects_legacy_or_unknown_fields() -> None:
    parsed = LifecycleStateV3.from_contract(contract())
    assert parsed.stage == "broker_simulation"
    assert parsed.contract_version == 3

    legacy = contract()
    legacy["contract_version"] = 2
    with pytest.raises(ValueError, match="v3"):
        LifecycleStateV3.from_contract(legacy)

    unknown = contract()
    unknown["gateway_password"] = "forbidden"
    with pytest.raises(ValueError, match="v3"):
        LifecycleStateV3.from_contract(unknown)


def test_v3_inserts_broker_simulation_without_changing_v2_transition_behavior() -> None:
    assert valid_transition_v3("shadow", "broker_simulation")
    assert valid_transition_v3("broker_simulation", "gray")
    assert not valid_transition_v3("shadow", "gray")

    authority_v2 = LifecycleAuthority(
        digest("b"), digest("c"), digest("d"), "shadow", 7
    )
    request = LifecycleRequest.master(
        "legacy-v2", "gray", authority_v2.snapshot(), 1_000, 2_000
    )
    assert authority_v2.apply(request, 1_500, gates=()).accepted


def test_only_deterministic_vnpy_admission_can_create_broker_simulation_ready() -> None:
    authority = LifecycleAuthorityV3(
        candidate_digest=digest("a"),
        package_digest=digest("b"),
        configuration_digest=digest("c"),
        policy_digest=digest("d"),
        evidence_bundle_digest=digest("e"),
        stage="shadow",
        revision=7,
    )
    result = authority.admit_broker_simulation(admission())
    assert result.accepted
    assert result.status == "broker_simulation_ready"
    assert result.producer_identity == "vnpy:model-lifecycle"
    assert authority.snapshot().stage == "broker_simulation"

    direct = LifecycleRequest.master(
        "master-cannot-admit", "broker_simulation", authority.legacy_snapshot(), 1_000, 2_000
    )
    denied = authority.apply_request(direct, 1_500, gates=())
    assert not denied.accepted
    assert "AUTOMATIC_ADMISSION_REQUIRED" in denied.reason_codes


def test_admission_fails_closed_on_wrong_stage_identity_or_uncertain_state() -> None:
    authority = LifecycleAuthorityV3(
        candidate_digest=digest("a"),
        package_digest=digest("b"),
        configuration_digest=digest("c"),
        policy_digest=digest("d"),
        evidence_bundle_digest=digest("e"),
        stage="paper",
        revision=7,
    )
    wrong_stage = authority.admit_broker_simulation(admission())
    assert not wrong_stage.accepted
    assert "BROKER_SIMULATION_WRONG_STAGE" in wrong_stage.reason_codes

    authority = LifecycleAuthorityV3(
        candidate_digest=digest("a"),
        package_digest=digest("b"),
        configuration_digest=digest("c"),
        policy_digest=digest("d"),
        evidence_bundle_digest=digest("e"),
        stage="shadow",
        revision=7,
    )
    blocked = authority.admit_broker_simulation(
        replace(admission(), unresolved_outcomes=1, metric_owner="model")
    )
    assert not blocked.accepted
    assert blocked.status == "blocked"
    assert "MODEL_OUTCOME_UNCERTAIN" in blocked.reason_codes
    assert "METRICS_NOT_HARNESS_OWNED" in blocked.reason_codes
