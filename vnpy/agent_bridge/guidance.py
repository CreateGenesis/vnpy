"""Typed vn.py bridge boundary for durable Side Master guidance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from time import time_ns
from typing import Any, Protocol


class _NativeGuidance(Protocol):
    def publish_guidance_request(
        self, payload: bytes, correlation_id: str, event_time_ms: int, expiry_ms: int
    ) -> int: ...
    def consume_guidance(self, now_ms: int) -> str | None: ...
    def ack_guidance(self, producer: str, epoch: int, sequence: int, at_ms: int) -> None: ...
    def replay_guidance_pending(self) -> int: ...
    def fetch_guidance_artifact(self, artifact_ref: str) -> bytes: ...
    def guidance_health(self) -> str: ...


@dataclass(frozen=True)
class GuidanceDelivery:
    frame_type: str
    schema_id: str
    producer_id: str
    producer_epoch: int
    sequence: int
    correlation_id: str
    event_time_ms: int
    expiry_ms: int
    replayed: bool
    payload: dict[str, Any]


DurableApply = Callable[[GuidanceDelivery], bool | None]


class NativeGuidanceBridge:
    """Decode strictly and ACK only after the caller durably applies the event."""

    def __init__(self, *, native: _NativeGuidance) -> None:
        self._native = native

    def publish_request(
        self,
        payload: Mapping[str, Any],
        correlation_id: str,
        event_time_ms: int,
        expiry_ms: int,
    ) -> int:
        if payload.get("contract_version") != 1:
            raise ValueError("guidance payload requires contract_version 1")
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return self._native.publish_guidance_request(
            encoded, correlation_id, event_time_ms, expiry_ms
        )

    def consume(
        self,
        now_ms: int,
        durable_apply: DurableApply,
        *,
        applied_at_ms: int | None = None,
    ) -> GuidanceDelivery | None:
        raw = self._native.consume_guidance(now_ms)
        if raw is None:
            return None
        delivery = _decode_delivery(raw)
        if delivery.expiry_ms >= 0 and delivery.expiry_ms < now_ms:
            raise ValueError("guidance delivery expired")
        if durable_apply(delivery) is False:
            raise RuntimeError("durable guidance application was rejected")
        self._native.ack_guidance(
            delivery.producer_id,
            delivery.producer_epoch,
            delivery.sequence,
            applied_at_ms if applied_at_ms is not None else time_ns() // 1_000_000,
        )
        return delivery

    def replay_pending(self) -> int:
        return self._native.replay_guidance_pending()

    def fetch_artifact(self, artifact_ref: str) -> bytes:
        if not artifact_ref.startswith("blake3:") or len(artifact_ref) != 71:
            raise ValueError("invalid guidance artifact reference")
        return self._native.fetch_guidance_artifact(artifact_ref)

    def health(self) -> dict[str, Any]:
        value = json.loads(self._native.guidance_health(), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(value, dict) or value.get("authority") != "research_only":
            raise ValueError("invalid guidance health response")
        return value


def _decode_delivery(raw: str) -> GuidanceDelivery:
    value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(value, dict) or value.get("contract_version") != 2:
        raise ValueError("unsupported guidance transport contract")
    if value.get("frame_type") not in {"operator_guidance", "guidance_ack"}:
        raise ValueError("invalid guidance frame type")
    if value.get("schema_id") not in {"operator-guid-v1", "guidance-ack-v1"}:
        raise ValueError("guidance schema mismatch")
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("contract_version") != 1:
        raise ValueError("invalid guidance payload")
    return GuidanceDelivery(
        frame_type=value["frame_type"],
        schema_id=value["schema_id"],
        producer_id=_hex_identity(value.get("producer_id"), "producer_id"),
        producer_epoch=_positive_int(value.get("producer_epoch"), "producer_epoch"),
        sequence=_positive_int(value.get("sequence"), "sequence"),
        correlation_id=_hex_identity(value.get("correlation_id"), "correlation_id"),
        event_time_ms=_integer(value.get("event_time_ms"), "event_time_ms"),
        expiry_ms=_integer(value.get("expiry_ms"), "expiry_ms"),
        replayed=value.get("replayed") if isinstance(value.get("replayed"), bool) else _bad("replayed"),
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
    bytes.fromhex(value)
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


def _bad(field: str) -> Any:
    raise ValueError(f"invalid {field}")
