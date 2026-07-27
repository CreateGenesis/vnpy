"""Production model transport over the model-only Rust PyO3 bridge.

The Python mmap ring remains available for research traffic only. Model input
and decision traffic must pass through this module so an ACK can only be sent
after the caller's durable application callback succeeds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
from time import time_ns
from typing import Any, Protocol


MODEL_INPUT_SCHEMA_ID = "model-input-v1__".encode().hex()
MODEL_DECISION_SCHEMA_ID = "model-decisionv1".encode().hex()


class _NativeBridge(Protocol):
    def publish_model_input(
        self, payload_json: bytes, correlation_id: str, event_time_ms: int, expiry_ms: int
    ) -> int: ...

    def publish_model_decision(
        self, payload_json: bytes, correlation_id: str, event_time_ms: int, expiry_ms: int
    ) -> int: ...

    def consume_model_input(self, now_ms: int) -> str | None: ...

    def consume_model_decision(self, now_ms: int) -> str | None: ...

    def ack_model_input(
        self, producer_id: str, producer_epoch: int, sequence: int, applied_at_ms: int
    ) -> None: ...

    def ack_model_decision(
        self, producer_id: str, producer_epoch: int, sequence: int, applied_at_ms: int
    ) -> None: ...

    def replay_model_pending(self) -> int: ...

    def replay_model_input_pending(self) -> int: ...

    def model_input_recovery_complete(
        self, correlation_id: str, event_time_ms: int
    ) -> int: ...

    def model_decision_recovery_complete(
        self, correlation_id: str, event_time_ms: int
    ) -> int: ...


@dataclass(frozen=True)
class ModelTransportDelivery:
    """One validated model frame awaiting durable application and ACK."""

    frame_type: str
    schema_id: str
    producer_id: str
    producer_epoch: int
    sequence: int
    correlation_id: str
    event_time_ms: int
    expiry_ms: int
    replayed: bool
    payload: dict[str, Any] | None

    @property
    def recovery_complete(self) -> bool:
        return self.frame_type == "recovery_complete"


DurableApply = Callable[[ModelTransportDelivery], bool | None]


class NativeModelBridge:
    """Typed model transport with ACK-after-durable-apply semantics."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        native: _NativeBridge | None = None,
        critical_capacity: int = 8_192,
    ) -> None:
        if native is not None:
            self._native = native
            return
        if root is None:
            raise ValueError("root is required when no native model transport is supplied")
        module = importlib.import_module("vnpy_bridge_py")
        self._native = module.NativeModelTransport(
            str(root),
            "vnpy-to-agentd",
            1,
            critical_capacity,
        )

    def publish_model_input(
        self,
        payload: Mapping[str, Any],
        correlation_id: str,
        event_time_ms: int,
        expiry_ms: int,
    ) -> int:
        return self._native.publish_model_input(
            _encode_payload(payload), correlation_id, event_time_ms, expiry_ms
        )

    def publish_model_decision(
        self,
        payload: Mapping[str, Any],
        correlation_id: str,
        event_time_ms: int,
        expiry_ms: int,
    ) -> int:
        return self._native.publish_model_decision(
            _encode_payload(payload), correlation_id, event_time_ms, expiry_ms
        )

    def consume_model_input(
        self,
        now_ms: int,
        durable_apply: DurableApply,
        *,
        applied_at_ms: int | None = None,
    ) -> ModelTransportDelivery | None:
        return self._consume_and_apply(
            self._native.consume_model_input(now_ms),
            MODEL_INPUT_SCHEMA_ID,
            durable_apply,
            self._native.ack_model_input,
            applied_at_ms,
        )

    def consume_model_decision(
        self,
        now_ms: int,
        durable_apply: DurableApply,
        *,
        applied_at_ms: int | None = None,
    ) -> ModelTransportDelivery | None:
        return self._consume_and_apply(
            self._native.consume_model_decision(now_ms),
            MODEL_DECISION_SCHEMA_ID,
            durable_apply,
            self._native.ack_model_decision,
            applied_at_ms,
        )

    def replay_pending(self) -> int:
        """Publish only journaled frames that are not already mmap-resident."""

        return self._native.replay_model_pending()

    def replay_input_pending(self) -> int:
        """Replay only vn.py-owned model inputs after producer restart."""

        return self._native.replay_model_input_pending()

    def publish_input_recovery_complete(
        self, correlation_id: str, event_time_ms: int
    ) -> int:
        return self._native.model_input_recovery_complete(correlation_id, event_time_ms)

    def publish_decision_recovery_complete(
        self, correlation_id: str, event_time_ms: int
    ) -> int:
        return self._native.model_decision_recovery_complete(correlation_id, event_time_ms)

    @staticmethod
    def _consume_and_apply(
        raw: str | None,
        expected_schema_id: str,
        durable_apply: DurableApply,
        ack: Callable[[str, int, int, int], None],
        applied_at_ms: int | None,
    ) -> ModelTransportDelivery | None:
        if raw is None:
            return None
        delivery = _decode_delivery(raw, expected_schema_id)
        applied = durable_apply(delivery)
        if applied is False:
            raise RuntimeError("durable model delivery application was rejected")
        ack(
            delivery.producer_id,
            delivery.producer_epoch,
            delivery.sequence,
            applied_at_ms if applied_at_ms is not None else time_ns() // 1_000_000,
        )
        return delivery


def _encode_payload(payload: Mapping[str, Any]) -> bytes:
    if payload.get("contract_version") != 1:
        raise ValueError("model payload requires contract_version 1")
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _decode_delivery(raw: str, expected_schema_id: str) -> ModelTransportDelivery:
    value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict) or value.get("contract_version") != 2:
        raise ValueError("unsupported model transport contract")
    frame_type = value.get("frame_type")
    if frame_type not in {"model_input", "model_decision", "recovery_complete"}:
        raise ValueError("invalid model frame type")
    if value.get("schema_id") != expected_schema_id:
        raise ValueError("model frame schema mismatch")
    payload = value.get("payload")
    if frame_type == "recovery_complete":
        if payload is not None:
            raise ValueError("RecoveryComplete must not carry a payload")
    elif not isinstance(payload, dict) or payload.get("contract_version") != 1:
        raise ValueError("invalid typed model payload")

    producer_id = _hex_identity(value.get("producer_id"), "producer_id")
    correlation_id = _hex_identity(value.get("correlation_id"), "correlation_id")
    producer_epoch = _positive_int(value.get("producer_epoch"), "producer_epoch")
    sequence = _positive_int(value.get("sequence"), "sequence")
    event_time_ms = _integer(value.get("event_time_ms"), "event_time_ms")
    expiry_ms = _integer(value.get("expiry_ms"), "expiry_ms")
    replayed = value.get("replayed")
    if not isinstance(replayed, bool):
        raise ValueError("replayed must be boolean")
    return ModelTransportDelivery(
        frame_type=frame_type,
        schema_id=expected_schema_id,
        producer_id=producer_id,
        producer_epoch=producer_epoch,
        sequence=sequence,
        correlation_id=correlation_id,
        event_time_ms=event_time_ms,
        expiry_ms=expiry_ms,
        replayed=replayed,
        payload=payload,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _hex_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 32:
        raise ValueError(f"invalid {field}")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    return value


def _positive_int(value: object, field: str) -> int:
    result = _integer(value, field)
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value
