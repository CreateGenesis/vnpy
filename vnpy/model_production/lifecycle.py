"""Canonical vn.py-owned lifecycle v2 state machine and production eligibility."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


_NEXT = {
    "generated": "pre_training_review",
    "pre_training_review": "training_or_calibration",
    "training_or_calibration": "evaluated",
    "evaluated": "simulation",
    "simulation": "paper",
    "paper": "shadow",
    "shadow": "gray",
    "gray": "production",
}


@dataclass(frozen=True)
class LifecycleRequestV2:
    request_id: str
    correlation_id: str
    idempotency_key: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    evidence_bundle_digest: str
    current_revision: int
    created_at_ms: int
    expires_at_ms: int
    requester_identity: str
    operation: str
    current_stage: str
    requested_stage: str
    payload_digest: str
    contract_version: int = 2
    entity_type: str = "model_lifecycle_request"

    @classmethod
    def from_contract(cls, value: dict[str, Any]) -> LifecycleRequestV2:
        required = {
            "contract_version", "entity_type", "request_id", "correlation_id",
            "idempotency_key", "package_digest", "configuration_digest", "policy_digest",
            "evidence_bundle_digest", "current_revision", "created_at_ms", "expires_at_ms",
            "requester_identity", "operation", "current_stage", "requested_stage", "payload_digest",
        }
        if set(value) != required or value.get("contract_version") != 2:
            raise ValueError("legacy or malformed lifecycle contract")
        if value.get("entity_type") != "model_lifecycle_request":
            raise ValueError("legacy or malformed lifecycle contract")
        return cls(**value)


@dataclass(frozen=True)
class LifecycleSnapshot:
    package_digest: str
    configuration_digest: str
    policy_digest: str
    stage: str
    revision: int


@dataclass(frozen=True)
class LifecycleRequest:
    request_id: str
    requester_identity: str
    requested_stage: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    current_stage: str
    current_revision: int
    created_at_ms: int
    expires_at_ms: int

    @classmethod
    def master(
        cls,
        request_id: str,
        requested_stage: str,
        current: LifecycleSnapshot,
        created_at_ms: int,
        expires_at_ms: int,
    ) -> LifecycleRequest:
        return cls(
            request_id, "master", requested_stage, current.package_digest,
            current.configuration_digest, current.policy_digest, current.stage,
            current.revision, created_at_ms, expires_at_ms,
        )


@dataclass(frozen=True)
class LifecycleDecision:
    accepted: bool
    status: str
    reason_codes: tuple[str, ...]
    applied_revision: int | None
    producer_identity: str = "vnpy:model-lifecycle"


@dataclass(frozen=True)
class ProductionEligibility:
    gray_sessions: int
    eligible_decisions: int
    reconciled: bool
    hard_limit_breaches: int
    unknown_outcomes: int
    safety_vetoes: int
    accepted_master_request_id: str
    fresh_review: bool = True
    fresh_exact_gates: bool = True

    def reason_codes(self, prior_master_request_id: str) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.gray_sessions < 10:
            reasons.append("GRAY_SESSIONS_INSUFFICIENT")
        if self.eligible_decisions < 200:
            reasons.append("GRAY_DECISIONS_INSUFFICIENT")
        if not self.reconciled:
            reasons.append("RECONCILIATION_REQUIRED")
        if self.hard_limit_breaches:
            reasons.append("HARD_LIMIT_BREACH")
        if self.unknown_outcomes:
            reasons.append("UNKNOWN_OUTCOME_BLOCK")
        if self.safety_vetoes:
            reasons.append("SAFETY_VETO_ACTIVE")
        if not self.fresh_review:
            reasons.append("RELEASE_REVIEW_STALE")
        if not self.fresh_exact_gates:
            reasons.append("PRODUCTION_GATES_STALE")
        if self.accepted_master_request_id == prior_master_request_id:
            reasons.append("FRESH_MASTER_REQUEST_REQUIRED")
        return tuple(reasons)


class LifecycleAuthority:
    def __init__(
        self,
        package_digest: str,
        configuration_digest: str,
        policy_digest: str,
        stage: str,
        revision: int,
    ) -> None:
        self._snapshot = LifecycleSnapshot(
            package_digest, configuration_digest, policy_digest, stage, revision
        )

    def snapshot(self) -> LifecycleSnapshot:
        return self._snapshot

    def apply(
        self,
        request: LifecycleRequest,
        now_ms: int,
        gates: tuple[str, ...],
    ) -> LifecycleDecision:
        reasons = list(gates)
        if request.requester_identity != "master":
            reasons.append("REQUESTER_UNAUTHORIZED")
        if now_ms < request.created_at_ms or now_ms >= request.expires_at_ms:
            reasons.append("REQUEST_EXPIRED")
        if (
            request.package_digest != self._snapshot.package_digest
            or request.configuration_digest != self._snapshot.configuration_digest
            or request.policy_digest != self._snapshot.policy_digest
            or request.current_stage != self._snapshot.stage
            or request.current_revision != self._snapshot.revision
        ):
            reasons.append("LIFECYCLE_IDENTITY_DRIFT")
        if _NEXT.get(self._snapshot.stage) != request.requested_stage:
            reasons.append("TRANSITION_UNDEFINED")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return LifecycleDecision(False, "rejected", tuple(reasons), None)
        self._snapshot = replace(
            self._snapshot,
            stage=request.requested_stage,
            revision=self._snapshot.revision + 1,
        )
        return LifecycleDecision(True, "accepted", (), self._snapshot.revision)
