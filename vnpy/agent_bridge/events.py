"""Versioned research-event contracts shared with agentd."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import IntEnum
import hashlib
import json
import math
import re
from time import time_ns
from typing import Any
from uuid import uuid4

from blake3 import blake3


class EventPriority(IntEnum):
    ROUTINE = 1
    CRITICAL = 2


@dataclass(frozen=True)
class AgentEvent:
    event_type: str
    payload: dict[str, Any]
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    event_id: str = field(default_factory=lambda: str(uuid4()))
    producer_id: str = "vnpy"
    producer_epoch: int = 1
    sequence: int = 0
    event_time_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    expiry_ms: int = -1
    priority: EventPriority = EventPriority.ROUTINE
    contract_version: int = 1

    def encode(self) -> bytes:
        value = asdict(self)
        value["priority"] = int(self.priority)
        return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @classmethod
    def decode(cls, data: bytes) -> "AgentEvent":
        value = json.loads(data)
        if value.get("contract_version") != 1:
            raise ValueError("unsupported Agent event contract")
        value["priority"] = EventPriority(value["priority"])
        return cls(**deepcopy(value))


LIVE_VALIDATION_EVENT_TYPES = {
    "live_validation.campaign": "campaign",
    "live_validation.route": "route",
    "live_validation.case": "case",
    "live_validation.call": "call",
    "live_validation.budget": "budget",
    "live_validation.tikhub_provenance": "tikhub_provenance",
    "live_validation.scorecard": "scorecard",
    "live_validation.audit": "audit",
    "live_validation.failure": "failure",
    "live_validation.improvement": "improvement",
    "live_validation.final": "final",
}
LIVE_VALIDATION_PAGE_KINDS = frozenset(LIVE_VALIDATION_EVENT_TYPES.values())

_LIVE_EVENT_FIELDS = {
    "contract_version",
    "entity_type",
    "event_id",
    "event_type",
    "campaign_id",
    "candidate_digest",
    "correlation_id",
    "producer_id",
    "producer_epoch",
    "revision",
    "event_time_ms",
    "payload",
    "previous_payload_digest",
    "payload_digest",
}
_LIVE_PAGE_FIELDS = {
    "page_kind",
    "page_index",
    "page_size",
    "next_cursor",
    "items",
    "certainty",
    "freshness",
    "error_code",
    "evidence_refs",
    "permitted_next_actions",
}
_LIVE_ACK_FIELDS = {
    "contract_version",
    "entity_type",
    "consumer",
    "event_id",
    "campaign_id",
    "candidate_digest",
    "producer_epoch",
    "revision",
    "page_kind",
    "page_index",
    "payload_digest",
    "status",
    "error_code",
    "received_at_ms",
    "authority",
    "provider_calls",
}
_LIVE_DIGEST = re.compile(r"^(?:blake3|sha256):[0-9a-f]{64}$")
_LIVE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LIVE_ERROR = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_LIVE_ACTION = re.compile(r"^[a-z][a-z0-9._-]{0,95}$")
_LIVE_SENSITIVE_KEYS = {
    "api_key",
    "access_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "raw_body",
    "raw_headers",
    "prompt",
    "model_supplied_score",
    "self_score",
}
_LIVE_SENSITIVE_VALUES = (
    "bearer ",
    "authorization:",
    "-----begin private key-----",
    "canary_secret",
)


class LiveValidationContractError(ValueError):
    """Stable fail-closed validation error for a projection envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LiveValidationContractError("DUPLICATE_FIELD", f"duplicate field: {key}")
        result[key] = value
    return result


def _canonical_live_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LiveValidationContractError("MALFORMED_PROJECTION", str(error)) from error


def compute_live_validation_payload_digest(
    payload: dict[str, Any],
    algorithm: str = "blake3",
) -> str:
    """Return the algorithm-qualified digest used by the Rust publisher."""
    encoded = _canonical_live_json(payload)
    if algorithm == "blake3":
        return f"blake3:{blake3(encoded).hexdigest()}"
    if algorithm == "sha256":
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    raise LiveValidationContractError("UNSUPPORTED_DIGEST", "unsupported digest algorithm")


def _bounded_unique_strings(
    value: Any,
    *,
    maximum: int,
    pattern: re.Pattern[str],
) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and len(value) == len(set(value))
        and all(isinstance(item, str) and pattern.fullmatch(item) for item in value)
    )


def _safe_summary_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        lowered = value.lower()
        return len(value) <= 512 and not any(marker in lowered for marker in _LIVE_SENSITIVE_VALUES)
    if isinstance(value, list):
        return len(value) <= 64 and all(
            not isinstance(item, (list, dict)) and _safe_summary_value(item) for item in value
        )
    return False


def _validate_live_page(payload: Any, event_type: str) -> None:
    if not isinstance(payload, dict) or set(payload) != _LIVE_PAGE_FIELDS:
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid page fields")
    if payload["page_kind"] != LIVE_VALIDATION_EVENT_TYPES[event_type]:
        raise LiveValidationContractError("PAGE_KIND_MISMATCH", "event/page kind mismatch")
    if (
        not isinstance(payload["page_index"], int)
        or isinstance(payload["page_index"], bool)
        or payload["page_index"] < 0
        or not isinstance(payload["page_size"], int)
        or isinstance(payload["page_size"], bool)
        or not 1 <= payload["page_size"] <= 100
        or not isinstance(payload["items"], list)
        or len(payload["items"]) > payload["page_size"]
    ):
        raise LiveValidationContractError("PAGE_BOUND_EXCEEDED", "invalid page bounds")
    cursor = payload["next_cursor"]
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 256):
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid cursor")
    for item in payload["items"]:
        if not isinstance(item, dict) or len(item) > 64:
            raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid page item")
        for key, value in item.items():
            if not isinstance(key, str) or key.lower() in _LIVE_SENSITIVE_KEYS:
                raise LiveValidationContractError("REDACTION_FAILED", "forbidden item field")
            if not _safe_summary_value(value):
                raise LiveValidationContractError("REDACTION_FAILED", "unsafe item value")
    if payload["certainty"] not in {"certain", "partial", "uncertain", "unknown"}:
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid certainty")
    if payload["freshness"] not in {"fresh", "stale", "expired", "unavailable"}:
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid freshness")
    error_code = payload["error_code"]
    if error_code is not None and (
        not isinstance(error_code, str) or not _LIVE_ERROR.fullmatch(error_code)
    ):
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid error code")
    if not _bounded_unique_strings(
        payload["evidence_refs"], maximum=64, pattern=_LIVE_DIGEST
    ):
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid evidence references")
    if not _bounded_unique_strings(
        payload["permitted_next_actions"], maximum=16, pattern=_LIVE_ACTION
    ):
        raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid next actions")


@dataclass(frozen=True)
class LiveValidationEvent:
    contract_version: int
    entity_type: str
    event_id: str
    event_type: str
    campaign_id: str
    candidate_digest: str
    correlation_id: str
    producer_id: str
    producer_epoch: int
    revision: int
    event_time_ms: int
    payload: dict[str, Any]
    previous_payload_digest: str | None
    payload_digest: str

    @property
    def page_key(self) -> str:
        return f"{self.payload['page_kind']}:{self.payload['page_index']}"

    def encode(self) -> bytes:
        return _canonical_live_json(asdict(self))

    @classmethod
    def decode(cls, data: bytes | str | dict[str, Any]) -> "LiveValidationEvent":
        if isinstance(data, dict):
            value = data
        else:
            try:
                value = json.loads(data, object_pairs_hook=_strict_json_object)
            except LiveValidationContractError:
                raise
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise LiveValidationContractError("MALFORMED_PROJECTION", str(error)) from error
        if not isinstance(value, dict) or set(value) != _LIVE_EVENT_FIELDS:
            raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid event fields")
        if value["contract_version"] != 1 or value["entity_type"] != "live_validation_event":
            raise LiveValidationContractError("INCOMPATIBLE_CONTRACT", "unsupported event contract")
        event_type = value["event_type"]
        if event_type not in LIVE_VALIDATION_EVENT_TYPES:
            raise LiveValidationContractError("MALFORMED_PROJECTION", "unknown event kind")
        for key in ("event_id", "campaign_id", "correlation_id", "producer_id"):
            if not isinstance(value[key], str) or not _LIVE_ID.fullmatch(value[key]):
                raise LiveValidationContractError("MALFORMED_PROJECTION", f"invalid {key}")
        if not isinstance(value["candidate_digest"], str) or not _LIVE_DIGEST.fullmatch(
            value["candidate_digest"]
        ):
            raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid candidate digest")
        for key in ("producer_epoch", "revision", "event_time_ms"):
            if (
                not isinstance(value[key], int)
                or isinstance(value[key], bool)
                or value[key] <= 0
            ):
                raise LiveValidationContractError("MALFORMED_PROJECTION", f"invalid {key}")
        _validate_live_page(value["payload"], event_type)
        claimed = value["payload_digest"]
        if not isinstance(claimed, str) or not _LIVE_DIGEST.fullmatch(claimed):
            raise LiveValidationContractError("MALFORMED_PROJECTION", "invalid payload digest")
        expected = compute_live_validation_payload_digest(
            value["payload"], claimed.split(":", 1)[0]
        )
        if claimed != expected:
            raise LiveValidationContractError("DIGEST_MISMATCH", "payload digest mismatch")
        previous = value["previous_payload_digest"]
        if value["revision"] == 1:
            if previous is not None:
                raise LiveValidationContractError(
                    "PROJECTION_CHAIN_MISMATCH", "first revision cannot name a predecessor"
                )
        elif not isinstance(previous, str) or not _LIVE_DIGEST.fullmatch(previous):
            raise LiveValidationContractError(
                "PROJECTION_CHAIN_MISMATCH", "later revision requires a predecessor"
            )
        return cls(**deepcopy(value))


@dataclass(frozen=True)
class LiveValidationAck:
    contract_version: int
    entity_type: str
    consumer: str
    event_id: str
    campaign_id: str
    candidate_digest: str
    producer_epoch: int
    revision: int
    page_kind: str
    page_index: int
    payload_digest: str
    status: str
    error_code: str | None
    received_at_ms: int
    authority: str = "research_only"
    provider_calls: int = 0

    @classmethod
    def create(
        cls,
        event: LiveValidationEvent,
        *,
        status: str,
        error_code: str | None,
        received_at_ms: int,
    ) -> "LiveValidationAck":
        if status not in {"applied", "duplicate", "stale_rejected", "invalid_rejected"}:
            raise LiveValidationContractError("MALFORMED_ACK", "invalid ACK status")
        if error_code is not None and not _LIVE_ERROR.fullmatch(error_code):
            raise LiveValidationContractError("MALFORMED_ACK", "invalid ACK error")
        return cls(
            contract_version=1,
            entity_type="live_validation_consumer_ack",
            consumer="vnpy",
            event_id=event.event_id,
            campaign_id=event.campaign_id,
            candidate_digest=event.candidate_digest,
            producer_epoch=event.producer_epoch,
            revision=event.revision,
            page_kind=event.payload["page_kind"],
            page_index=event.payload["page_index"],
            payload_digest=event.payload_digest,
            status=status,
            error_code=error_code,
            received_at_ms=max(1, received_at_ms),
        )

    def encode(self) -> bytes:
        return _canonical_live_json(asdict(self))

    @classmethod
    def decode(cls, data: bytes | str | dict[str, Any]) -> "LiveValidationAck":
        value = data if isinstance(data, dict) else json.loads(data, object_pairs_hook=_strict_json_object)
        if not isinstance(value, dict) or set(value) != _LIVE_ACK_FIELDS:
            raise LiveValidationContractError("MALFORMED_ACK", "invalid ACK fields")
        ack = cls(**deepcopy(value))
        if (
            ack.contract_version != 1
            or ack.entity_type != "live_validation_consumer_ack"
            or ack.consumer != "vnpy"
            or ack.authority != "research_only"
            or ack.provider_calls != 0
            or ack.status not in {"applied", "duplicate", "stale_rejected", "invalid_rejected"}
            or not _LIVE_ID.fullmatch(ack.event_id)
            or not _LIVE_ID.fullmatch(ack.campaign_id)
            or not _LIVE_DIGEST.fullmatch(ack.candidate_digest)
            or not _LIVE_DIGEST.fullmatch(ack.payload_digest)
            or ack.page_kind not in LIVE_VALIDATION_PAGE_KINDS
            or ack.producer_epoch <= 0
            or ack.revision <= 0
            or ack.page_index < 0
            or ack.received_at_ms <= 0
        ):
            raise LiveValidationContractError("MALFORMED_ACK", "invalid ACK")
        if ack.error_code is not None and not _LIVE_ERROR.fullmatch(ack.error_code):
            raise LiveValidationContractError("MALFORMED_ACK", "invalid ACK error")
        return ack
