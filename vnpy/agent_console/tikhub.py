"""Immutable, redacted TikHub operational projection types."""

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping


TIKHUB_EVENT_TYPES = frozenset(
    {
        "tikhub.catalog",
        "tikhub.health",
        "tikhub.account",
        "tikhub.budget",
        "tikhub.mission",
        "tikhub.page",
        "tikhub.result",
        "tikhub.audit",
        "tikhub.route",
        "tikhub.security",
        "tikhub.cutover",
    }
)

_FIELDS = {
    "tikhub.catalog": frozenset(("state", "catalog_digest", "version", "platforms", "drift", "audit_state", "error_code")),
    "tikhub.health": frozenset(("state", "route_mode", "checked_at_ms", "latency_ms", "provider_request_id", "error_code")),
    "tikhub.account": frozenset(("credential_readiness", "scope_verdict", "balance", "free_credit", "observed_usage", "unit_price", "currency", "usage_state", "price_state", "freshness_ms", "unknown", "error_code")),
    "tikhub.budget": frozenset(("ledger_revision", "status", "mission_cap", "global_cap", "reserve", "reserved", "reconciled", "uncertain", "spendable_after_reserve", "currency", "error_code")),
    "tikhub.mission": frozenset(("mission_id", "state", "endpoint_id", "pages_requested", "pages_completed", "items", "completeness", "terminal_evidence_ref", "error_code")),
    "tikhub.page": frozenset(("mission_id", "operation_id", "page_index", "state", "outcome_certainty", "items", "duplicates", "pagination_state_digest", "raw_artifact_ref", "normalized_artifact_ref", "error_code")),
    "tikhub.result": frozenset(("mission_id", "status", "record_count", "normalized_artifact_ref", "evidence_ref", "untrusted", "completeness")),
    "tikhub.audit": frozenset(("catalog_digest", "decision", "complexity", "reviewer_count", "approvals", "safety_vetoes", "expires_at_ms", "evidence_ref")),
    "tikhub.route": frozenset(("route_policy_ref", "mode", "state", "remote_dns", "leak_check", "exit_identity", "attested_at_ms", "error_code")),
    "tikhub.security": frozenset(("control", "status", "secret_lookup_performed", "process_started", "network_started", "evidence_ref", "error_code")),
    "tikhub.cutover": frozenset(("status", "legacy_assets", "legacy_registry", "tikhub_mcp_processes", "mcp_tikhub_egress", "fallback_attempts", "generic_mcp_regression", "evidence_ref")),
}

_FORBIDDEN = ("authorization", "bearer ", "api_key", "cursor", "raw_content", "response_body")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class _TikHubPayloadView:
    payload: Mapping[str, Any]

    @classmethod
    def create(cls, payload: dict[str, Any]) -> "_TikHubPayloadView":
        return cls(_freeze(payload))

    def as_dict(self) -> dict[str, Any]:
        return _thaw(self.payload)


@dataclass(frozen=True)
class TikHubCatalogView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubHealthView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubAccountView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubBudgetView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubMissionView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubPageView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubResultView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubAuditView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubRouteView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubSecurityView(_TikHubPayloadView):
    pass


@dataclass(frozen=True)
class TikHubCutoverView(_TikHubPayloadView):
    pass


_VIEW_TYPES = {
    "tikhub.catalog": ("catalog", TikHubCatalogView),
    "tikhub.health": ("health", TikHubHealthView),
    "tikhub.account": ("account", TikHubAccountView),
    "tikhub.budget": ("budget", TikHubBudgetView),
    "tikhub.mission": ("mission", TikHubMissionView),
    "tikhub.page": ("page", TikHubPageView),
    "tikhub.result": ("result", TikHubResultView),
    "tikhub.audit": ("audit", TikHubAuditView),
    "tikhub.route": ("route", TikHubRouteView),
    "tikhub.security": ("security", TikHubSecurityView),
    "tikhub.cutover": ("cutover", TikHubCutoverView),
}


@dataclass(frozen=True)
class TikHubViewState:
    revision: int = 0
    correlation_id: str | None = None
    source_revisions: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    catalog: TikHubCatalogView | None = None
    health: TikHubHealthView | None = None
    account: TikHubAccountView | None = None
    budget: TikHubBudgetView | None = None
    mission: TikHubMissionView | None = None
    page: TikHubPageView | None = None
    result: TikHubResultView | None = None
    audit: TikHubAuditView | None = None
    route: TikHubRouteView | None = None
    security: TikHubSecurityView | None = None
    cutover: TikHubCutoverView | None = None
    last_error: str | None = None

    def apply(
        self,
        event_type: str,
        payload: dict[str, Any],
        correlation_id: str,
        contract_version: int,
    ) -> "TikHubViewState":
        next_revision = self.revision + 1
        if contract_version != 1:
            return replace(self, revision=next_revision, last_error="incompatible TikHub event")
        revision = payload.get("revision")
        body = {key: value for key, value in payload.items() if key != "revision"}
        if not isinstance(revision, int) or revision < 1:
            return replace(self, revision=next_revision, last_error="invalid TikHub revision")
        field_name, view_type = _VIEW_TYPES[event_type]
        if revision <= self.source_revisions.get(field_name, 0):
            return replace(self, revision=next_revision, last_error="stale TikHub event")
        encoded = repr(body).lower()
        if set(body) != _FIELDS[event_type] or any(token in encoded for token in _FORBIDDEN):
            return replace(self, revision=next_revision, last_error="invalid redacted TikHub payload")
        if event_type == "tikhub.result" and body.get("untrusted") is not True:
            return replace(self, revision=next_revision, last_error="invalid TikHub trust label")
        revisions = dict(self.source_revisions)
        revisions[field_name] = revision
        return replace(
            self,
            revision=next_revision,
            correlation_id=correlation_id,
            source_revisions=MappingProxyType(revisions),
            **{field_name: view_type.create(body)},
            last_error=None,
        )

    def console_payload(self) -> dict[str, Any]:
        values = {
            field_name: getattr(self, field_name).as_dict() if getattr(self, field_name) else {}
            for field_name, _ in _VIEW_TYPES.values()
        }
        errors = sorted(
            {
                value["error_code"]
                for value in values.values()
                if isinstance(value, dict) and value.get("error_code")
            }
        )
        artifact_refs = sorted(
            {
                item
                for value in values.values()
                for key, item in value.items()
                if (key.endswith("_ref") or key.endswith("_artifact_ref"))
                and isinstance(item, str)
            }
        )
        return {
            "revision": self.revision,
            "correlation_id": self.correlation_id,
            "source_revisions": dict(self.source_revisions),
            **values,
            "errors": errors,
            "artifacts": artifact_refs,
            "last_error": self.last_error,
        }
