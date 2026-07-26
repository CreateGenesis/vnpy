"""Opaque research lifecycle envelopes; vn.py remains the sole decision authority."""

from dataclasses import asdict, dataclass, replace
from hashlib import blake2b
import json
from time import time_ns
from typing import Literal
from uuid import uuid4

import blake3

from .events import AgentEvent, EventPriority


LifecycleStatus = Literal["accepted", "rejected", "blocked", "expired", "unavailable"]


@dataclass(frozen=True)
class LifecycleRequest:
    request_id: str
    correlation_id: str
    strategy_artifact: str
    requested_stage: str
    observer_gate_digest: str
    created_at_ms: int
    expires_at_ms: int
    payload_hash: str
    contract_version: int = 1


@dataclass(frozen=True)
class LifecycleGateResult:
    request_id: str
    status: LifecycleStatus
    reason: str
    producer_identity: str = "vnpy:autonomous-control"
    event_time_ms: int = 0
    payload_hash: str = ""
    applied_lifecycle_revision: int | None = None
    contract_version: int = 1


@dataclass(frozen=True)
class GuidanceStrategyLifecycleReceipt:
    """vn.py-owned strategy-version lifecycle evidence for guidance retention."""

    mission_id: str
    notification_id: str
    strategy_id: str
    strategy_version: str
    state: Literal["active", "terminated"]
    lifecycle_revision: int
    event_time_ms: int
    terminated_at_ms: int | None
    receipt_digest: str
    producer_identity: str = "vnpy:autonomous-control"
    contract_version: int = 1


def _payload_hash(request: LifecycleRequest) -> str:
    payload = {
        "contract_version": request.contract_version,
        "request_id": request.request_id,
        "correlation_id": request.correlation_id,
        "strategy_artifact": request.strategy_artifact,
        "requested_stage": request.requested_stage,
        "observer_gate_digest": request.observer_gate_digest,
        "created_at_ms": request.created_at_ms,
        "expires_at_ms": request.expires_at_ms,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"blake2b:{blake2b(encoded, digest_size=32).hexdigest()}"


def create_lifecycle_request(
    strategy_artifact: str,
    requested_stage: str,
    observer_gate_digest: str,
    *,
    now_ms: int | None = None,
    ttl_ms: int = 60_000,
) -> LifecycleRequest:
    if not observer_gate_digest:
        raise ValueError("passed observer gate evidence is required")
    if not strategy_artifact or not requested_stage or ttl_ms <= 0:
        raise ValueError("lifecycle request identity and expiry are required")
    now_ms = time_ns() // 1_000_000 if now_ms is None else now_ms
    request = LifecycleRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        strategy_artifact=strategy_artifact,
        requested_stage=requested_stage,
        observer_gate_digest=observer_gate_digest,
        created_at_ms=now_ms,
        expires_at_ms=now_ms + ttl_ms,
        payload_hash="",
    )
    return replace(request, payload_hash=_payload_hash(request))


def lifecycle_request_event(request: LifecycleRequest, *, now_ms: int | None = None) -> AgentEvent:
    now_ms = time_ns() // 1_000_000 if now_ms is None else now_ms
    if request.contract_version != 1 or request.expires_at_ms <= now_ms:
        raise ValueError("lifecycle request is stale or incompatible")
    if _payload_hash(replace(request, payload_hash="")) != request.payload_hash:
        raise ValueError("lifecycle request payload hash mismatch")
    return AgentEvent(
        event_type="lifecycle.request",
        payload=asdict(request),
        correlation_id=request.correlation_id,
        expiry_ms=request.expires_at_ms,
        priority=EventPriority.CRITICAL,
    )


def correlate_vnpy_result(
    request: LifecycleRequest,
    result: LifecycleGateResult,
    *,
    now_ms: int | None = None,
) -> LifecycleGateResult:
    now_ms = time_ns() // 1_000_000 if now_ms is None else now_ms
    if result.producer_identity != "vnpy:autonomous-control":
        return LifecycleGateResult(request.request_id, "blocked", "untrusted_result_producer")
    if result.contract_version != request.contract_version:
        return LifecycleGateResult(request.request_id, "blocked", "incompatible_result")
    if result.request_id != request.request_id or result.payload_hash != request.payload_hash:
        return LifecycleGateResult(request.request_id, "blocked", "result_identity_mismatch")
    if request.expires_at_ms <= now_ms or result.event_time_ms >= request.expires_at_ms:
        return LifecycleGateResult(request.request_id, "blocked", "stale_result")
    if result.status == "accepted" and result.applied_lifecycle_revision is None:
        return LifecycleGateResult(request.request_id, "blocked", "missing_lifecycle_revision")
    return result


def create_guidance_strategy_lifecycle_receipt(
    mission_id: str,
    notification_id: str,
    strategy_id: str,
    strategy_version: str,
    state: Literal["active", "terminated"],
    lifecycle_revision: int,
    *,
    event_time_ms: int | None = None,
    terminated_at_ms: int | None = None,
) -> GuidanceStrategyLifecycleReceipt:
    """Create content-free evidence without granting Agent lifecycle authority."""

    event_time_ms = time_ns() // 1_000_000 if event_time_ms is None else event_time_ms
    if not all((mission_id, notification_id, strategy_id, strategy_version)):
        raise ValueError("guidance strategy lifecycle identity is required")
    if state not in ("active", "terminated") or lifecycle_revision <= 0 or event_time_ms < 0:
        raise ValueError("guidance strategy lifecycle state is invalid")
    if state == "active" and terminated_at_ms is not None:
        raise ValueError("active strategy cannot carry termination time")
    if state == "terminated":
        terminated_at_ms = event_time_ms if terminated_at_ms is None else terminated_at_ms
        if terminated_at_ms < 0 or terminated_at_ms > event_time_ms:
            raise ValueError("strategy termination time is invalid")
    receipt = GuidanceStrategyLifecycleReceipt(
        mission_id=mission_id,
        notification_id=notification_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        state=state,
        lifecycle_revision=lifecycle_revision,
        event_time_ms=event_time_ms,
        terminated_at_ms=terminated_at_ms,
        receipt_digest="",
    )
    return replace(receipt, receipt_digest=_guidance_receipt_digest(receipt))


def validate_guidance_strategy_lifecycle_receipt(
    receipt: GuidanceStrategyLifecycleReceipt,
) -> None:
    """Fail closed on producer substitution, malformed time, or digest changes."""

    if receipt.producer_identity != "vnpy:autonomous-control" or receipt.contract_version != 1:
        raise ValueError("untrusted guidance strategy lifecycle producer")
    expected = create_guidance_strategy_lifecycle_receipt(
        receipt.mission_id,
        receipt.notification_id,
        receipt.strategy_id,
        receipt.strategy_version,
        receipt.state,
        receipt.lifecycle_revision,
        event_time_ms=receipt.event_time_ms,
        terminated_at_ms=receipt.terminated_at_ms,
    )
    if expected.receipt_digest != receipt.receipt_digest:
        raise ValueError("guidance strategy lifecycle digest mismatch")


def _guidance_receipt_digest(receipt: GuidanceStrategyLifecycleReceipt) -> str:
    value = asdict(receipt)
    value.pop("receipt_digest", None)
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"blake3:{blake3.blake3(canonical).hexdigest()}"
