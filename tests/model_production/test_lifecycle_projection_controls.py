from __future__ import annotations

from vnpy.agent_console.controls import (
    OperatorPolicyEnvelope,
    model_policy_envelope_control,
    model_safety_control,
)
from vnpy.agent_console.model_lifecycle import ModelLifecycleViewState


def test_lifecycle_projection_and_operator_safety_controls_expose_no_routine_approval() -> None:
    view = ModelLifecycleViewState.from_projection({
        "contract_version": 2, "entity_type": "model_lifecycle_projection", "revision": 8,
        "package_digest": "blake3:" + "a" * 64, "stage": "gray",
        "gate_states": {"observer": "passed"}, "gray_remaining": {"messages": 90},
        "broker_outcomes": {"accepted": 1}, "reconciliation_state": "complete",
        "incidents": [], "emergency_stop": False, "rollback_state": None,
        "permitted_next_actions": ["inspect", "stop"],
        "last_request_id": "request-gray", "last_decision_status": "accepted",
        "stop_state": None,
    })
    assert view.stage == "gray" and view.gray_remaining["messages"] == 90
    event = model_safety_control("disable_new_admission", "model-production", 4)
    assert event.payload["routine_approval"] is False
    assert "approve" not in event.payload
    configured = model_policy_envelope_control(
        OperatorPolicyEnvelope(4, ("600000.SH",), 100, 25, 10, 2_000)
    )
    assert configured.payload["max_total_exposure_bps"] == 100
    assert configured.payload["routine_approval"] is False
