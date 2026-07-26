import json

import pytest

from vnpy.agent_bridge.guidance import NativeGuidanceBridge


class FakeNative:
    def __init__(self) -> None:
        self.acked: list[tuple[str, int, int, int]] = []
        self.published: list[tuple[bytes, str, int, int]] = []

    def consume_guidance(self, _now_ms: int) -> str:
        return json.dumps({
            "contract_version": 2, "frame_type": "operator_guidance",
            "schema_id": "operator-guid-v1", "producer_id": "01" * 16,
            "producer_epoch": 1, "sequence": 2, "correlation_id": "02" * 16,
            "event_time_ms": 100, "expiry_ms": 200, "replayed": False,
            "payload": {"contract_version": 1, "notification_id": "n-1"},
        })

    def ack_guidance(self, producer: str, epoch: int, sequence: int, at_ms: int) -> None:
        self.acked.append((producer, epoch, sequence, at_ms))

    def replay_guidance_pending(self) -> int:
        return 1

    def publish_guidance_request(
        self, payload: bytes, correlation_id: str, event_time_ms: int, expiry_ms: int
    ) -> int:
        self.published.append((payload, correlation_id, event_time_ms, expiry_ms))
        return 3

    def fetch_guidance_artifact(self, artifact_ref: str) -> bytes:
        assert artifact_ref == "blake3:" + "aa" * 32
        return b"artifact"

    def guidance_health(self) -> str:
        return json.dumps({"status": "healthy", "authority": "research_only"})


class InvalidNative(FakeNative):
    def __init__(self, raw: str) -> None:
        super().__init__()
        self.raw = raw

    def consume_guidance(self, _now_ms: int) -> str:
        return self.raw


def test_ack_occurs_only_after_durable_apply() -> None:
    native = FakeNative()
    bridge = NativeGuidanceBridge(native=native)
    delivery = bridge.consume(101, lambda value: value.payload["notification_id"] == "n-1", applied_at_ms=110)
    assert delivery.sequence == 2
    assert native.acked == [("01" * 16, 1, 2, 110)]
    assert bridge.replay_pending() == 1


def test_publish_artifact_and_health_are_typed_native_calls() -> None:
    native = FakeNative()
    bridge = NativeGuidanceBridge(native=native)
    sequence = bridge.publish_request(
        {"contract_version": 1, "body": {"free": ["json", 1]}},
        "correlation-1",
        100,
        200,
    )
    assert sequence == 3
    assert json.loads(native.published[0][0]) == {
        "body": {"free": ["json", 1]},
        "contract_version": 1,
    }
    assert bridge.fetch_artifact("blake3:" + "aa" * 32) == b"artifact"
    assert bridge.health()["authority"] == "research_only"


@pytest.mark.parametrize(
    "raw",
    [
        '{"contract_version":3}',
        '{"contract_version":2,"contract_version":2}',
        json.dumps({
            "contract_version": 2, "frame_type": "operator_guidance",
            "schema_id": "unknown-v1", "producer_id": "01" * 16,
            "producer_epoch": 1, "sequence": 2, "correlation_id": "02" * 16,
            "event_time_ms": 100, "expiry_ms": 200, "replayed": False,
            "payload": {"contract_version": 1},
        }),
    ],
)
def test_malformed_or_incompatible_delivery_never_acknowledges(raw: str) -> None:
    native = InvalidNative(raw)
    bridge = NativeGuidanceBridge(native=native)
    with pytest.raises(ValueError):
        bridge.consume(101, lambda _value: True)
    assert native.acked == []
