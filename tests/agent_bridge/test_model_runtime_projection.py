from __future__ import annotations

from copy import deepcopy

import pytest

from vnpy.agent_console.model_runtime import ModelRuntimeViewState


def digest(character: str) -> str:
    return "blake3:" + character * 64


def projection() -> dict:
    return {
        "contract_version": 1,
        "entity_type": "model_runtime_projection",
        "revision": 4,
        "raw_interest_count": 120,
        "qualified_wakeup_count": 3,
        "fast_action_count": 8,
        "model_identity": {
            "package_digest": digest("a"),
            "runtime_slot": "modeld:slot-a",
            "lifecycle_revision": 7,
            "stage": "gray",
        },
        "latency": {"inference_p999_ms": 12.5, "end_to_end_p999_ms": 41.0},
        "risk": {
            "accepted_count": 6,
            "rejected_count": 2,
            "reason_counts": {"T1_SELLABLE_INSUFFICIENT": 2},
        },
        "redacted_intents": [
            {
                "intent_id": "intent-1",
                "decision_id": "decision-1",
                "symbol": "600000.SH",
                "action": "sell",
                "disposition": "rejected",
                "reason_codes": ["T1_SELLABLE_INSUFFICIENT"],
                "latency_ns": 41_000_000,
                "evidence_digest": digest("b"),
            }
        ],
        "evidence_refs": [digest("c")],
    }


def test_runtime_projection_exposes_counts_identity_latency_risk_and_redacted_intents() -> None:
    view = ModelRuntimeViewState().apply_projection(projection())
    assert view.raw_interest_count == 120
    assert view.qualified_wakeup_count == 3
    assert view.fast_action_count == 8
    assert view.package_digest == digest("a")
    assert view.end_to_end_p999_ms == 41.0
    assert view.rejection_reasons == {"T1_SELLABLE_INSUFFICIENT": 2}
    assert view.intents[0].symbol == "600000.SH"


@pytest.mark.parametrize("field", ["raw_market_payload", "account_cash", "api_key", "send_order"])
def test_runtime_projection_rejects_unredacted_or_mutating_fields(field: str) -> None:
    payload = projection()
    payload[field] = "forbidden"
    with pytest.raises(ValueError, match="unredacted"):
        ModelRuntimeViewState().apply_projection(payload)


def test_runtime_projection_rejects_stale_revision_and_unredacted_intent_fields() -> None:
    current = ModelRuntimeViewState().apply_projection(projection())
    with pytest.raises(ValueError, match="stale"):
        current.apply_projection(projection())
    payload = deepcopy(projection())
    payload["revision"] = 5
    payload["redacted_intents"][0]["limit_price_micros"] = 10_000_000
    with pytest.raises(ValueError, match="not redacted"):
        current.apply_projection(payload)
