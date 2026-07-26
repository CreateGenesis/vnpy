"""Credential-safe live evaluation and Harness baseline projections."""

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any

from vnpy.agent_bridge.events import LiveValidationEvent

from .models import LiveValidationTypedPage


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


@dataclass(frozen=True)
class LiveValidationViewState:
    """Paged, typed, last-known-valid live-validation read model."""

    campaign_id: str | None = None
    candidate_digest: str | None = None
    producer_epoch: int = 0
    revision: int = 0
    pages: dict[str, LiveValidationTypedPage] = field(default_factory=dict)
    updated_at_ms: int = 0
    projection_latency_ms: int = 0
    last_error: str | None = None

    def apply(
        self,
        value: bytes | str | dict[str, Any] | LiveValidationEvent,
        *,
        rendered_at_ms: int | None = None,
    ) -> "LiveValidationViewState":
        event = value if isinstance(value, LiveValidationEvent) else LiveValidationEvent.decode(value)
        if self.campaign_id is not None and event.campaign_id != self.campaign_id:
            return replace(self, last_error="CAMPAIGN_MISMATCH")
        if self.candidate_digest is not None and event.candidate_digest != self.candidate_digest:
            return replace(self, last_error="CANDIDATE_DRIFT")
        if event.producer_epoch < self.producer_epoch:
            return replace(self, last_error="STALE_PRODUCER_EPOCH")
        page = LiveValidationTypedPage.from_event(event)
        key = f"{page.page_kind}:{page.page_index}"
        previous = self.pages.get(key)
        if previous is not None:
            if page.revision < previous.revision:
                return replace(self, last_error="STALE_REVISION")
            if page.revision == previous.revision:
                code = (
                    "DUPLICATE_PROJECTION"
                    if page.payload_digest == previous.payload_digest
                    else "REVISION_COLLISION"
                )
                return replace(self, last_error=code)
            if page.revision != previous.revision + 1:
                return replace(self, last_error="OUT_OF_ORDER_REVISION")
            if page.previous_payload_digest != previous.payload_digest:
                return replace(self, last_error="PROJECTION_CHAIN_MISMATCH")
        elif page.revision != 1:
            return replace(self, last_error="PROJECTION_CHAIN_MISMATCH")
        pages = dict(self.pages)
        pages[key] = page
        rendered = rendered_at_ms if rendered_at_ms is not None else time_ns() // 1_000_000
        return replace(
            self,
            campaign_id=event.campaign_id,
            candidate_digest=event.candidate_digest,
            producer_epoch=max(self.producer_epoch, event.producer_epoch),
            revision=self.revision + 1,
            pages=pages,
            updated_at_ms=max(1, rendered),
            projection_latency_ms=max(0, rendered - event.event_time_ms),
            last_error=None,
        )

    def page(
        self,
        page_kind: str,
        page_index: int = 0,
        *,
        query: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        page = self.pages.get(f"{page_kind}:{page_index}")
        if page is None:
            return ()
        values = tuple(item.as_dict() for item in page.items)
        if query is None or not query.strip():
            return values
        needle = query.casefold()
        return tuple(item for item in values if needle in str(item).casefold())

    def console_payload(self) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {
            kind: []
            for kind in (
                "campaign",
                "route",
                "case",
                "call",
                "budget",
                "tikhub_provenance",
                "scorecard",
                "audit",
                "failure",
                "improvement",
                "final",
            )
        }
        metadata: dict[str, list[dict[str, Any]]] = {kind: [] for kind in grouped}
        for page in sorted(self.pages.values(), key=lambda value: (value.page_kind, value.page_index)):
            grouped[page.page_kind].extend(item.as_dict() for item in page.items)
            metadata[page.page_kind].append(
                {
                    "page_index": page.page_index,
                    "page_size": page.page_size,
                    "next_cursor": page.next_cursor,
                    "revision": page.revision,
                    "certainty": page.certainty,
                    "freshness": page.freshness,
                    "error_code": page.error_code,
                    "evidence_refs": list(page.evidence_refs),
                    "permitted_next_actions": list(page.permitted_next_actions),
                }
            )
        low_watermarks = [
            item
            for item in grouped["budget"]
            if item.get("low_watermark_state") not in {None, "normal"}
        ]
        errors = sorted(
            {
                page.error_code
                for page in self.pages.values()
                if page.error_code is not None
            }
        )
        next_actions = sorted(
            {
                action
                for page in self.pages.values()
                for action in page.permitted_next_actions
            }
        )
        return {
            "campaign_id": self.campaign_id,
            "candidate_digest": self.candidate_digest,
            "producer_epoch": self.producer_epoch,
            "revision": self.revision,
            "views": grouped,
            "pages": metadata,
            "budget_low_watermarks": low_watermarks,
            "errors": errors,
            "permitted_next_actions": next_actions,
            "projection_latency_ms": self.projection_latency_ms,
            "last_error": self.last_error,
            "authority": "research_only",
            "provider_calls": 0,
        }
