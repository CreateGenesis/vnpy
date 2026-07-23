"""Credential-safe live evaluation and Harness baseline projections."""

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any


_EVENT_TARGETS = {
    "evaluation.run": "run",
    "evaluation.budget": "budget",
    "evaluation.scorecard": "scorecard",
    "evaluation.failure": "failure",
    "evaluation.audit": "audit",
    "evaluation.comparison": "comparison",
    "evaluation.shadow": "shadow",
    "evaluation.baseline": "baseline",
}

_ALLOWED_FIELDS = {
    "run": {
        "revision",
        "run_id",
        "suite_id",
        "suite_version",
        "status",
        "completed_cases",
        "total_cases",
        "role",
        "model",
        "endpoint_fingerprint",
        "manifest_digest",
        "route_digest",
        "prompt_digest",
        "workflow_digest",
        "skill_digests",
        "cli_contract_digests",
        "resource_profile_digest",
        "rubric_digest",
        "budget_revision",
        "live_transport_attested",
        "usage",
        "reserved",
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
    "scorecard": {
        "revision",
        "score",
        "task_completion",
        "cli_skill_contract",
        "evidence_traceability",
        "safety_pass",
        "contract_pass",
        "score_producer",
        "scorer_version",
        "evidence_bundle_digest",
        "rubric_digest",
        "objective_inputs",
    },
    "failure": {"revision", "code", "disposition", "http_status", "diagnostic"},
    "audit": {
        "revision",
        "subject_digest",
        "reviewer_identities",
        "reviewer_key_fingerprints",
        "signature_verified",
        "approvals",
        "ordinary_rejections",
        "veto",
        "quorum",
        "evidence_digest",
        "audit_prompt_digest",
        "audit_policy_digest",
    },
    "comparison": {
        "revision",
        "candidate_profile_digest",
        "baseline_profile_digest",
        "no_regression",
        "regressions",
        "task_deltas",
        "capability_deltas",
        "resource_deltas",
        "safety_pass",
        "contract_pass",
        "comparison_digest",
    },
    "shadow": {
        "revision",
        "completed",
        "required",
        "no_regression",
        "hard_failures",
        "comparison_digest",
    },
    "baseline": {
        "revision",
        "active_profile_digest",
        "previous_profile_digest",
        "candidate_state",
        "rollback_available",
        "registry_revision",
        "suite_digest",
        "route_digest",
        "resource_profile_digest",
    },
}

_SENSITIVE_MARKERS = (
    "authorization",
    "api_key",
    "api-key",
    "access_key",
    "private_context",
    "raw_prompt",
    "raw_response",
)


@dataclass(frozen=True)
class EvaluationViewState:
    revision: int = 0
    correlation_id: str | None = None
    source_revisions: dict[str, int] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    scorecard: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)
    shadow: dict[str, Any] = field(default_factory=dict)
    baseline: dict[str, Any] = field(default_factory=dict)
    projection_latency_ms: int = 0
    last_error: str | None = None

    def apply(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "EvaluationViewState":
        now_ms = time_ns() // 1_000_000
        target = _EVENT_TARGETS.get(event_type)
        if target is None:
            return replace(
                self,
                revision=self.revision + 1,
                correlation_id=correlation_id,
                projection_latency_ms=max(0, now_ms - event_time_ms),
                last_error=f"unknown evaluation event: {event_type}",
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
                last_error=f"stale evaluation event: {event_type}",
            )
        sanitized = {
            key: _sanitize_value(value)
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


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_value(item)
            for key, item in value.items()
            if not _sensitive_key(str(key))
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
