"""Opaque research lifecycle envelopes; vn.py remains the sole decision authority."""

from dataclasses import asdict, dataclass, replace
from hashlib import blake2b
import json
from time import time_ns
from typing import Literal
from uuid import uuid4

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
