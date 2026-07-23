"""Thread-safe, UI-independent Agent Console read models."""

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any


_EVENT_FIELDS = {
    "bridge.health": "bridge_health",
    "bridge.lanes": "lanes",
    "bridge.latency": "latency",
    "bridge.diagnostic": "diagnostics",
    "observer.gate": "observer_gate",
    "budget.ledger": "budgets",
    "mission.state": "missions",
    "workflow.state": "workflows",
    "subagent.state": "subagents",
    "qualification.state": "qualifications",
    "evaluation.state": "evaluations",
    "route.state": "routes",
    "wakeup.state": "wakeups",
    "capability.state": "capabilities",
    "sandbox.denial": "sandbox_denials",
    "mcp.state": "mcp",
    "secret_broker.state": "secret_broker",
    "grant.state": "grants",
    "artifact.state": "artifacts",
    "audit.state": "audits",
    "score.input": "score_inputs",
    "harness.state": "harness",
    "shadow.state": "shadow",
    "coverage.state": "coverage",
    "recovery.state": "recovery",
    "lifecycle.request": "lifecycle_requests",
    "lifecycle.result": "lifecycle_results",
}


@dataclass(frozen=True)
class ConsoleState:
    revision: int = 0
    updated_at_ms: int = field(default_factory=lambda: time_ns() // 1_000_000)
    projection_latency_ms: int = 0
    bridge_health: str = "unavailable"
    correlation_id: str | None = None
    source_revisions: dict[str, int] = field(default_factory=dict)
    lanes: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    observer_gate: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    missions: dict[str, Any] = field(default_factory=dict)
    workflows: dict[str, Any] = field(default_factory=dict)
    subagents: dict[str, Any] = field(default_factory=dict)
    qualifications: dict[str, Any] = field(default_factory=dict)
    evaluations: dict[str, Any] = field(default_factory=dict)
    routes: dict[str, Any] = field(default_factory=dict)
    wakeups: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    sandbox_denials: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    secret_broker: dict[str, Any] = field(default_factory=dict)
    grants: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    audits: dict[str, Any] = field(default_factory=dict)
    score_inputs: dict[str, Any] = field(default_factory=dict)
    harness: dict[str, Any] = field(default_factory=dict)
    shadow: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    lifecycle_requests: dict[str, Any] = field(default_factory=dict)
    lifecycle_results: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None

    def apply(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "ConsoleState":
        now_ms = time_ns() // 1_000_000
        field_name = _EVENT_FIELDS.get(event_type)
        if field_name is None:
            return replace(
                self,
                revision=self.revision + 1,
                updated_at_ms=now_ms,
                projection_latency_ms=max(0, now_ms - event_time_ms),
                correlation_id=correlation_id,
                last_error=f"unknown research event: {event_type}",
            )

        source_revision = payload.get("revision", 0)
        if not isinstance(source_revision, int) or source_revision < 0:
            source_revision = 0
        previous_revision = self.source_revisions.get(field_name, -1)
        if source_revision <= previous_revision:
            return replace(
                self,
                revision=self.revision + 1,
                updated_at_ms=now_ms,
                projection_latency_ms=max(0, now_ms - event_time_ms),
                correlation_id=correlation_id,
                last_error=f"stale research event: {event_type}",
            )

        revisions = dict(self.source_revisions)
        revisions[field_name] = source_revision
        value: Any = payload.get("state", "unknown") if field_name == "bridge_health" else dict(payload)
        return replace(
            self,
            **{field_name: value},
            source_revisions=revisions,
            revision=self.revision + 1,
            updated_at_ms=now_ms,
            projection_latency_ms=max(0, now_ms - event_time_ms),
            correlation_id=correlation_id,
            last_error=None,
        )
