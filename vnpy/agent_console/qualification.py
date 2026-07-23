"""Credential-safe subagent qualification and delegation projections."""

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any


QUALIFICATION_EVENT_TYPES = frozenset(
    {
        "qualification.state",
        "qualification.campaign",
        "qualification.budget",
        "qualification.evidence",
        "qualification.failure",
        "qualification.remediation",
        "grant.state",
    }
)

_EVENT_TARGETS = {
    "qualification.state": "qualification",
    "qualification.campaign": "campaign",
    "qualification.budget": "budget",
    "qualification.evidence": "evidence",
    "qualification.failure": "failure_history",
    "qualification.remediation": "remediation",
    "grant.state": "grant",
}

_ALLOWED_FIELDS = {
    "qualification": {
        "revision",
        "qualification_id",
        "key",
        "key_digest",
        "run_ids",
        "scores",
        "evidence_digests",
        "minimum_score",
        "safety_pass",
        "contract_pass",
        "live_transport_attested",
        "state",
        "issued_at_ms",
        "valid_until_ms",
        "delegated_failures",
        "failure_history",
        "remediation",
    },
    "campaign": {
        "revision",
        "campaign_id",
        "key",
        "key_digest",
        "status",
        "completed_runs",
        "required_runs",
        "budget_revision",
        "reservation_held",
        "state_digest",
        "diagnostic",
    },
    "budget": {
        "revision",
        "hard_limit",
        "spent",
        "reserved",
        "remaining",
        "ledger_revision",
        "low_watermark",
    },
    "evidence": {
        "revision",
        "qualification_id",
        "run_ids",
        "scores",
        "evidence_digests",
        "safety_pass",
        "contract_pass",
        "live_transport_attested",
    },
    "failure_history": {
        "revision",
        "qualification_id",
        "delegated_failures",
        "failures",
        "state",
        "revoke_outstanding_grants",
    },
    "remediation": {
        "revision",
        "qualification_id",
        "task_class",
        "action",
        "reason",
        "requires_requalification",
    },
    "grant": {
        "revision",
        "grant_id",
        "qualification_key_digest",
        "mission_id",
        "step_id",
        "state",
        "expires_at",
        "resource_reservation",
        "budget_revision",
        "consumed_at",
    },
}

_QUALIFICATION_KEY_FIELDS = {
    "role",
    "endpoint_fingerprint",
    "model",
    "prompt_digest",
    "skill_digests",
    "cli_contract_digests",
    "contract_digest",
    "resource_profile_digest",
    "task_class",
    "suite_version",
}

_SENSITIVE_MARKERS = (
    "authorization",
    "api_key",
    "api-key",
    "access_key",
    "base_url",
    "raw_prompt",
    "raw_response",
    "private_context",
)


@dataclass(frozen=True)
class QualificationViewState:
    revision: int = 0
    correlation_id: str | None = None
    source_revisions: dict[str, int] = field(default_factory=dict)
    qualification: dict[str, Any] = field(default_factory=dict)
    campaign: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    failure_history: dict[str, Any] = field(default_factory=dict)
    remediation: dict[str, Any] = field(default_factory=dict)
    grant: dict[str, Any] = field(default_factory=dict)
    projection_latency_ms: int = 0
    last_error: str | None = None

    def apply(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "QualificationViewState":
        now_ms = time_ns() // 1_000_000
        target = _EVENT_TARGETS.get(event_type)
        if target is None:
            return replace(
                self,
                revision=self.revision + 1,
                correlation_id=correlation_id,
                projection_latency_ms=max(0, now_ms - event_time_ms),
                last_error=f"unknown qualification event: {event_type}",
            )
        source_revision = payload.get("revision", 0)
        if not isinstance(source_revision, int) or source_revision < 0:
            source_revision = 0
        if source_revision <= self.source_revisions.get(target, -1):
            return replace(
                self,
                revision=self.revision + 1,
                correlation_id=correlation_id,
                projection_latency_ms=max(0, now_ms - event_time_ms),
                last_error=f"stale qualification event: {event_type}",
            )
        sanitized = {
            key: _sanitize_value(value, key)
            for key, value in payload.items()
            if key in _ALLOWED_FIELDS[target] and not _sensitive_key(key)
        }
        revisions = dict(self.source_revisions)
        revisions[target] = source_revision
        return replace(
            self,
            **{target: sanitized},
            source_revisions=revisions,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            projection_latency_ms=max(0, now_ms - event_time_ms),
            last_error=None,
        )


def _sanitize_value(value: Any, parent: str = "") -> Any:
    if isinstance(value, dict):
        allowed = _QUALIFICATION_KEY_FIELDS if parent == "key" else None
        return {
            key: _sanitize_value(item, str(key))
            for key, item in value.items()
            if not _sensitive_key(str(key)) and (allowed is None or key in allowed)
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if "bearer " in lowered or any(marker in lowered for marker in _SENSITIVE_MARKERS):
            return "[REDACTED]"
        return value[:512]
    return value


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)
