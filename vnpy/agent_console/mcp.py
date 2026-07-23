"""Redacted MCP registry, import, call, and secret-broker read models."""

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any


_ALLOWED_IMPORT_FIELDS = {
    "status",
    "mode",
    "source_digest",
    "semantic_digest",
    "normalizer_version",
    "adapter_manifest_digest",
    "candidate_set_digest",
    "entry_count",
    "previous_active_digest",
    "process_started",
    "network_started",
    "secret_lookup_performed",
    "activated",
    "audit_state",
    "error_code",
}

MCP_EVENT_TYPES = {
    "mcp.state",
    "mcp.import",
    "mcp.registry",
    "mcp.call",
    "mcp.health",
    "mcp.audit",
    "secret_broker.state",
}

_ALLOWED_REGISTRY_FIELDS = {
    "catalog_digest",
    "server_id",
    "version",
    "entry_count",
    "component_digest",
    "endpoint_fingerprint",
    "lifecycle",
    "audit_decision_ref",
    "health",
    "previous_active_digest",
    "error_code",
}

_ALLOWED_CALL_FIELDS = {
    "method",
    "server_id",
    "tool_name",
    "correlation_id",
    "status",
    "untrusted",
    "provenance",
    "complete",
    "cursor",
    "usage_wall_ms",
    "evidence_refs",
    "implicit_context_injected",
}

_ALLOWED_AUDIT_FIELDS = {
    "catalog_digest",
    "route_model",
    "route_fingerprint",
    "supervisor_key_fingerprint",
    "reviewer_identities",
    "reviewer_key_fingerprints",
    "signature_verified",
    "approvals",
    "ordinary_rejections",
    "safety_vetoes",
    "quorum",
    "evidence_digest",
    "prompt_digest",
    "policy_digest",
    "lifecycle",
}

_ALLOWED_SECRET_FIELDS = {
    "available",
    "rotation_revision",
    "grant_id",
    "grant_state",
    "peer_verified",
    "consumed_once",
    "descriptor_closed",
    "mechanism",
    "outcome",
    "error_code",
}

_ALLOWED_SANDBOX_FIELDS = {
    "profile",
    "seccomp",
    "no_new_privileges",
    "effective_capabilities_zero",
    "isolated_user_namespace",
    "isolated_pid_namespace",
    "isolated_mount_namespace",
    "read_only_runtime",
    "workspace_mounted",
    "child_uid",
}


@dataclass(frozen=True)
class McpViewState:
    revision: int = 0
    correlation_id: str | None = None
    import_status: dict[str, Any] = field(default_factory=dict)
    registry: dict[str, Any] = field(default_factory=dict)
    calls: dict[str, Any] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    secret_broker: dict[str, Any] = field(default_factory=dict)
    projection_latency_ms: int = 0
    last_error: str | None = None

    def apply_import(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "McpViewState":
        sanitized = {key: payload[key] for key in _ALLOWED_IMPORT_FIELDS if key in payload}
        if sanitized.get("process_started") is not False or sanitized.get("network_started") is not False:
            return replace(
                self,
                revision=self.revision + 1,
                correlation_id=correlation_id,
                projection_latency_ms=max(0, time_ns() // 1_000_000 - event_time_ms),
                last_error="MCP compatibility import violated offline boundary",
            )
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            import_status=sanitized,
            projection_latency_ms=max(0, time_ns() // 1_000_000 - event_time_ms),
            last_error=None,
        )

    def apply_registry(
        self,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        event_time_ms: int | None = None,
    ) -> "McpViewState":
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id or self.correlation_id,
            registry={key: payload[key] for key in _ALLOWED_REGISTRY_FIELDS if key in payload},
            projection_latency_ms=_latency(event_time_ms),
            last_error=None,
        )

    def apply_call(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "McpViewState":
        if payload.get("implicit_context") is not False:
            return replace(
                self,
                revision=self.revision + 1,
                correlation_id=correlation_id,
                projection_latency_ms=_latency(event_time_ms),
                last_error="MCP call attempted implicit context injection",
            )
        raw_result = payload.get("result")
        result = raw_result if isinstance(raw_result, dict) else payload
        sanitized = {key: result[key] for key in _ALLOWED_CALL_FIELDS if key in result}
        sanitized["status"] = payload.get("status", sanitized.get("status", "unknown"))
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            sanitized["error_code"] = error["code"]
        sandbox = payload.get("sandbox")
        if isinstance(sandbox, dict):
            sanitized["sandbox"] = {
                key: sandbox[key] for key in _ALLOWED_SANDBOX_FIELDS if key in sandbox
            }
        secret = payload.get("secret_delivery")
        secret_broker = self.secret_broker
        if isinstance(secret, dict):
            secret_broker = {
                key: secret[key] for key in _ALLOWED_SECRET_FIELDS if key in secret
            }
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            calls=sanitized,
            secret_broker=secret_broker,
            projection_latency_ms=_latency(event_time_ms),
            last_error=None,
        )

    def apply_audit(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "McpViewState":
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            audit={key: payload[key] for key in _ALLOWED_AUDIT_FIELDS if key in payload},
            projection_latency_ms=_latency(event_time_ms),
            last_error=None,
        )

    def apply_secret_broker(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "McpViewState":
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            secret_broker={key: payload[key] for key in _ALLOWED_SECRET_FIELDS if key in payload},
            projection_latency_ms=_latency(event_time_ms),
            last_error=None,
        )

    def apply(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        event_time_ms: int,
    ) -> "McpViewState":
        if event_type == "mcp.state":
            kind = payload.get("kind")
            event_type = f"mcp.{kind}" if isinstance(kind, str) else event_type
        if event_type == "mcp.import":
            return self.apply_import(payload, correlation_id, event_time_ms)
        if event_type == "mcp.registry":
            return self.apply_registry(payload, correlation_id, event_time_ms)
        if event_type in {"mcp.call", "mcp.health"}:
            return self.apply_call(payload, correlation_id, event_time_ms)
        if event_type == "mcp.audit":
            return self.apply_audit(payload, correlation_id, event_time_ms)
        if event_type == "secret_broker.state":
            return self.apply_secret_broker(payload, correlation_id, event_time_ms)
        return replace(
            self,
            revision=self.revision + 1,
            correlation_id=correlation_id,
            projection_latency_ms=_latency(event_time_ms),
            last_error="unknown MCP projection event",
        )

    def console_payload(self) -> dict[str, Any]:
        return {
            "import": dict(self.import_status),
            "registry": dict(self.registry),
            "calls": dict(self.calls),
            "audit": dict(self.audit),
            "projection_latency_ms": self.projection_latency_ms,
            "last_error": self.last_error,
        }


def _latency(event_time_ms: int | None) -> int:
    if event_time_ms is None:
        return 0
    return max(0, time_ns() // 1_000_000 - event_time_ms)
