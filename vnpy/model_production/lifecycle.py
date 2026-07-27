"""Canonical vn.py-owned lifecycle state machines and production eligibility."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID


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

_NEXT_V3 = {
    **_NEXT,
    "shadow": "broker_simulation",
    "broker_simulation": "gray",
}

_V3_STAGES = frozenset({
    "generated",
    "pre_training_review",
    "training_or_calibration",
    "evaluated",
    "simulation",
    "paper",
    "shadow",
    "broker_simulation",
    "gray",
    "production",
    "stopped",
    "rolled_back",
    "retired",
})
_V3_STATES = frozenset({"inactive", "ready", "active", "blocked", "terminal"})


def _valid_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        algorithm, hexadecimal = value.split(":", 1)
    except ValueError:
        return False
    return (
        algorithm in {"blake3", "sha256"}
        and len(hexadecimal) == 64
        and all(character in "0123456789abcdef" for character in hexadecimal)
    )


def valid_transition_v3(current_stage: str, requested_stage: str) -> bool:
    """Return whether an ordinary v3 stage transition follows the canonical sequence."""
    return _NEXT_V3.get(current_stage) == requested_stage


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
class LifecycleStateV3:
    candidate_digest: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    evidence_bundle_digest: str
    stage: str
    revision: int
    state: str
    gateway_binding_digest: str | None = None
    campaign_id: str | None = None
    contract_version: int = 3
    entity_type: str = "model_lifecycle"

    @classmethod
    def from_contract(cls, value: dict[str, Any]) -> LifecycleStateV3:
        required = {
            "contract_version",
            "entity_type",
            "candidate_digest",
            "package_digest",
            "configuration_digest",
            "policy_digest",
            "evidence_bundle_digest",
            "stage",
            "revision",
            "state",
        }
        allowed = required | {"gateway_binding_digest", "campaign_id"}
        if not required.issubset(value) or not set(value).issubset(allowed):
            raise ValueError("invalid lifecycle v3 contract")
        parsed = cls(**value)
        if not parsed.is_valid():
            raise ValueError("invalid lifecycle v3 contract")
        return parsed

    def is_valid(self) -> bool:
        digests = (
            self.candidate_digest,
            self.package_digest,
            self.configuration_digest,
            self.policy_digest,
            self.evidence_bundle_digest,
        )
        if (
            self.contract_version != 3
            or self.entity_type != "model_lifecycle"
            or not all(_valid_digest(value) for value in digests)
            or self.stage not in _V3_STAGES
            or self.state not in _V3_STATES
            or self.revision < 0
        ):
            return False
        if self.gateway_binding_digest is not None and not _valid_digest(
            self.gateway_binding_digest
        ):
            return False
        if self.campaign_id is not None:
            try:
                if UUID(self.campaign_id).int == 0:
                    return False
            except (ValueError, AttributeError):
                return False
        has_campaign_binding = (
            self.gateway_binding_digest is not None or self.campaign_id is not None
        )
        return self.stage == "broker_simulation" or not has_campaign_binding


@dataclass(frozen=True)
class BrokerSimulationAdmissionEvidence:
    candidate_digest: str
    package_digest: str
    configuration_digest: str
    policy_digest: str
    evidence_bundle_digest: str
    metric_owner: str
    candidate_gate_passed: bool
    rqdata_tick_ready: bool
    observer_converged: bool
    runtime_ready: bool
    review_ready: bool
    hard_risk_ready: bool
    reconciliation_ready: bool
    gateway_bindings_ready: bool
    unresolved_outcomes: int
    hard_breaker_active: bool


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


class LifecycleAuthorityV3:
    """vn.py-only automatic broker-simulation admission authority."""

    def __init__(
        self,
        candidate_digest: str,
        package_digest: str,
        configuration_digest: str,
        policy_digest: str,
        evidence_bundle_digest: str,
        stage: str,
        revision: int,
    ) -> None:
        self._snapshot = LifecycleStateV3(
            candidate_digest=candidate_digest,
            package_digest=package_digest,
            configuration_digest=configuration_digest,
            policy_digest=policy_digest,
            evidence_bundle_digest=evidence_bundle_digest,
            stage=stage,
            revision=revision,
            state="active",
        )
        if not self._snapshot.is_valid():
            raise ValueError("invalid lifecycle v3 initial state")

    def snapshot(self) -> LifecycleStateV3:
        return self._snapshot

    def legacy_snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            package_digest=self._snapshot.package_digest,
            configuration_digest=self._snapshot.configuration_digest,
            policy_digest=self._snapshot.policy_digest,
            stage=self._snapshot.stage,
            revision=self._snapshot.revision,
        )

    def apply_request(
        self,
        request: LifecycleRequest,
        now_ms: int,
        gates: tuple[str, ...],
    ) -> LifecycleDecision:
        if request.requested_stage == "broker_simulation":
            return LifecycleDecision(
                False,
                "rejected",
                ("AUTOMATIC_ADMISSION_REQUIRED",),
                None,
            )
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
        if not valid_transition_v3(self._snapshot.stage, request.requested_stage):
            reasons.append("TRANSITION_UNDEFINED")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return LifecycleDecision(False, "rejected", tuple(reasons), None)
        self._snapshot = replace(
            self._snapshot,
            stage=request.requested_stage,
            revision=self._snapshot.revision + 1,
            state="active",
        )
        return LifecycleDecision(True, "accepted", (), self._snapshot.revision)

    def admit_broker_simulation(
        self,
        evidence: BrokerSimulationAdmissionEvidence,
    ) -> LifecycleDecision:
        reasons: list[str] = []
        if self._snapshot.stage != "shadow":
            reasons.append("BROKER_SIMULATION_WRONG_STAGE")
        if (
            evidence.candidate_digest != self._snapshot.candidate_digest
            or evidence.package_digest != self._snapshot.package_digest
            or evidence.configuration_digest != self._snapshot.configuration_digest
            or evidence.policy_digest != self._snapshot.policy_digest
            or evidence.evidence_bundle_digest != self._snapshot.evidence_bundle_digest
        ):
            reasons.append("LIFECYCLE_IDENTITY_DRIFT")
        if evidence.metric_owner != "harness":
            reasons.append("METRICS_NOT_HARNESS_OWNED")
        gates = (
            (evidence.candidate_gate_passed, "CANDIDATE_GATE_REQUIRED"),
            (evidence.rqdata_tick_ready, "RQDATA_TICK_REQUIRED"),
            (evidence.observer_converged, "OBSERVER_CONVERGENCE_REQUIRED"),
            (evidence.runtime_ready, "MODEL_RUNTIME_REQUIRED"),
            (evidence.review_ready, "REVIEW_EVIDENCE_REQUIRED"),
            (evidence.hard_risk_ready, "HARD_RISK_EVIDENCE_REQUIRED"),
            (evidence.reconciliation_ready, "RECONCILIATION_REQUIRED"),
            (evidence.gateway_bindings_ready, "SIMULATION_GATEWAY_BINDING_REQUIRED"),
        )
        reasons.extend(reason for passed, reason in gates if not passed)
        if evidence.unresolved_outcomes:
            reasons.append("MODEL_OUTCOME_UNCERTAIN")
        if evidence.hard_breaker_active:
            reasons.append("HARD_BREAKER_ACTIVE")
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            return LifecycleDecision(False, "blocked", tuple(reasons), None)
        self._snapshot = replace(
            self._snapshot,
            stage="broker_simulation",
            revision=self._snapshot.revision + 1,
            state="ready",
        )
        return LifecycleDecision(
            True,
            "broker_simulation_ready",
            (),
            self._snapshot.revision,
        )
