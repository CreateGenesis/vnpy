from pathlib import Path

import pytest

from vnpy.agent_bridge.autonomous_control import (
    LifecycleGateResult,
    correlate_vnpy_result,
    create_lifecycle_request,
    lifecycle_request_event,
)


def test_lifecycle_request_is_opaque_and_requires_passed_observer_gate() -> None:
    request = create_lifecycle_request(
        "artifact-digest", "simulation", "observer-gate-digest", now_ms=1_000
    )
    assert request.contract_version == 1
    assert request.requested_stage == "simulation"
    event = lifecycle_request_event(request, now_ms=1_001)
    assert event.event_type == "lifecycle.request"
    assert event.payload["strategy_artifact"] == "artifact-digest"
    with pytest.raises(ValueError):
        create_lifecycle_request("artifact-digest", "gray", "")


def test_only_matching_fresh_vnpy_result_is_authoritative() -> None:
    request = create_lifecycle_request(
        "artifact-digest", "simulation", "observer-gate", now_ms=1_000
    )
    accepted = LifecycleGateResult(
        request_id=request.request_id,
        status="accepted",
        reason="all_gates_passed",
        event_time_ms=1_002,
        payload_hash=request.payload_hash,
        applied_lifecycle_revision=7,
    )
    assert correlate_vnpy_result(request, accepted, now_ms=1_002) == accepted

    rejected = LifecycleGateResult(
        request_id=request.request_id,
        status="rejected",
        reason="risk_gate_failed",
        event_time_ms=1_003,
        payload_hash=request.payload_hash,
    )
    assert correlate_vnpy_result(request, rejected, now_ms=1_003).status == "rejected"

    stale = LifecycleGateResult(
        request_id=request.request_id,
        status="accepted",
        reason="late",
        event_time_ms=request.expires_at_ms,
        payload_hash=request.payload_hash,
        applied_lifecycle_revision=8,
    )
    assert correlate_vnpy_result(request, stale, now_ms=request.expires_at_ms).status == "blocked"


def test_result_spoofing_and_identity_drift_fail_closed() -> None:
    request = create_lifecycle_request("artifact", "simulation", "gate", now_ms=1_000)
    spoofed = LifecycleGateResult(
        request_id=request.request_id,
        status="accepted",
        reason="spoofed",
        producer_identity="agent:master",
        event_time_ms=1_001,
        payload_hash=request.payload_hash,
        applied_lifecycle_revision=1,
    )
    assert correlate_vnpy_result(request, spoofed, now_ms=1_001).status == "blocked"


def test_adapter_contains_no_direct_strategy_or_trading_calls() -> None:
    source = Path(__file__).parents[2] / "vnpy" / "agent_bridge" / "autonomous_control.py"
    text = source.read_text(encoding="utf-8")
    for prohibited in ("send_order(", "cancel_order(", "start_strategy(", "stop_strategy("):
        assert prohibited not in text
