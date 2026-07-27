import json
from typing import Any

import pytest

from vnpy.agent_bridge.native_bridge import (
    MODEL_DECISION_SCHEMA_ID,
    MODEL_INPUT_SCHEMA_ID,
    NativeModelBridge,
)


class FakeNativeBridge:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.decisions: list[str] = []
        self.calls: list[tuple[Any, ...]] = []

    def consume_model_input(self, _now_ms: int) -> str | None:
        return self.inputs.pop(0) if self.inputs else None

    def consume_model_decision(self, _now_ms: int) -> str | None:
        return self.decisions.pop(0) if self.decisions else None

    def ack_model_input(self, *args: Any) -> None:
        self.calls.append(("ack_input", *args))

    def ack_model_decision(self, *args: Any) -> None:
        self.calls.append(("ack_decision", *args))

    def replay_model_pending(self) -> int:
        self.calls.append(("replay",))
        return 2

    def replay_model_input_pending(self) -> int:
        self.calls.append(("replay_inputs",))
        return 1

    def model_input_recovery_complete(self, correlation_id: str, event_time_ms: int) -> int:
        self.calls.append(("input_recovery", correlation_id, event_time_ms))
        return 8

    def model_decision_recovery_complete(self, correlation_id: str, event_time_ms: int) -> int:
        self.calls.append(("decision_recovery", correlation_id, event_time_ms))
        return 9

    def publish_model_input(self, *args: Any) -> int:
        self.calls.append(("publish_input", *args))
        return 10

    def publish_model_decision(self, *args: Any) -> int:
        self.calls.append(("publish_decision", *args))
        return 11


def delivery(
    frame_type: str = "model_decision",
    schema_id: str = MODEL_DECISION_SCHEMA_ID,
    sequence: int = 3,
) -> str:
    return json.dumps(
        {
            "contract_version": 2,
            "frame_type": frame_type,
            "schema_id": schema_id,
            "producer_id": "11" * 16,
            "producer_epoch": 4,
            "sequence": sequence,
            "correlation_id": "22" * 16,
            "event_time_ms": 100,
            "expiry_ms": 200,
            "replayed": True,
            "payload": None
            if frame_type == "recovery_complete"
            else {"contract_version": 1, "decision_type": "hold"},
        }
    )


def test_ack_occurs_only_after_durable_apply() -> None:
    native = FakeNativeBridge()
    native.decisions.append(delivery())
    bridge = NativeModelBridge(native=native)

    def apply(item: Any) -> None:
        native.calls.append(("apply", item.sequence))

    result = bridge.consume_model_decision(101, apply, applied_at_ms=102)
    assert result and result.sequence == 3 and result.replayed
    assert native.calls == [
        ("apply", 3),
        ("ack_decision", "11" * 16, 4, 3, 102),
    ]


def test_failed_or_rejected_durable_apply_never_acks() -> None:
    native = FakeNativeBridge()
    native.decisions.extend((delivery(sequence=3), delivery(sequence=4)))
    bridge = NativeModelBridge(native=native)

    with pytest.raises(OSError):
        bridge.consume_model_decision(101, lambda _item: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(RuntimeError, match="rejected"):
        bridge.consume_model_decision(101, lambda _item: False)
    assert native.calls == []


def test_recovery_complete_is_durably_applied_and_acked_in_order() -> None:
    native = FakeNativeBridge()
    native.inputs.append(delivery("recovery_complete", MODEL_INPUT_SCHEMA_ID, 7))
    bridge = NativeModelBridge(native=native)
    applied: list[bool] = []

    result = bridge.consume_model_input(
        101,
        lambda item: applied.append(item.recovery_complete),
        applied_at_ms=103,
    )
    assert result and result.recovery_complete
    assert applied == [True]
    assert native.calls == [("ack_input", "11" * 16, 4, 7, 103)]


def test_schema_mismatch_is_rejected_before_application_or_ack() -> None:
    native = FakeNativeBridge()
    native.decisions.append(delivery(schema_id=MODEL_INPUT_SCHEMA_ID))
    bridge = NativeModelBridge(native=native)

    with pytest.raises(ValueError, match="schema mismatch"):
        bridge.consume_model_decision(101, lambda _item: None)
    assert native.calls == []


def test_replay_recovery_and_canonical_publication_delegate_to_native_bridge() -> None:
    native = FakeNativeBridge()
    bridge = NativeModelBridge(native=native)
    assert bridge.replay_pending() == 2
    assert bridge.replay_input_pending() == 1
    assert bridge.publish_input_recovery_complete("corr", 100) == 8
    assert bridge.publish_decision_recovery_complete("corr", 101) == 9
    assert (
        bridge.publish_model_input(
            {"contract_version": 1, "name": "model"}, "corr", 102, 200
        )
        == 10
    )
    assert native.calls[-1][0] == "publish_input"
    assert native.calls[-1][1] == b'{"contract_version":1,"name":"model"}'


def test_nonfinite_and_wrong_version_publication_fail_before_native_call() -> None:
    native = FakeNativeBridge()
    bridge = NativeModelBridge(native=native)
    with pytest.raises(ValueError, match="contract_version"):
        bridge.publish_model_input({"contract_version": 2}, "corr", 1, 2)
    with pytest.raises(ValueError):
        bridge.publish_model_input(
            {"contract_version": 1, "score": float("nan")}, "corr", 1, 2
        )
    assert native.calls == []
