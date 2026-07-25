"""Thread-safe, UI-independent Agent Console read models."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import re
from time import time_ns
from typing import Any
from unicodedata import normalize

from blake3 import blake3


_PROJECTION_SECTIONS = (
    "task",
    "workflow",
    "workers",
    "qualifications",
    "skills",
    "tools_and_cli",
    "pattern_similarity",
    "solidification",
    "stateless_audit",
    "memory",
    "resources",
    "recovery",
    "model_pipeline",
    "vnpy_authority",
)
_PROJECTION_REQUIRED = {
    "contract_version",
    "entity_type",
    "projection_id",
    "projection_revision",
    "projection_digest",
    "correlation_id",
    "authoritative_source_revisions",
    "created_at_ms",
    "expires_at_ms",
    *_PROJECTION_SECTIONS,
}
_PROJECTION_OPTIONAL = {"previous_projection_digest"}
_SECTION_REQUIRED = {
    "source_revision",
    "source_digest",
    "state",
    "certainty",
    "freshness",
    "summary",
    "evidence_refs",
    "permitted_next_actions",
    "updated_at_ms",
    "stale",
}
_SECTION_OPTIONAL = {"last_error_code"}
_DIGEST = re.compile(r"^(?:blake3|sha256):[0-9a-f]{64}$")
_SUMMARY_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SENSITIVE_KEYS = {
    "api_key",
    "access_key",
    "authorization",
    "password",
    "secret",
    "secret_value",
    "token",
    "cookie",
    "private_key",
    "raw_body",
    "raw_header",
    "raw_headers",
    "prompt",
}


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(normalize("NFC", value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite projection number")
        if value == 0.0:
            return "0.0"
        encoded = repr(value)
        if "e" in encoded:
            mantissa, exponent = encoded.split("e", 1)
            sign = ""
            if exponent[0] in "+-":
                sign, exponent = exponent[0], exponent[1:]
            encoded = f"{mantissa}e{sign}{int(exponent)}"
        return encoded
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("projection object key is not text")
            key = normalize("NFC", key)
            if key in normalized:
                raise ValueError("projection keys collide after normalization")
            normalized[key] = item
        return "{" + ",".join(
            f"{_canonical_json(key)}:{_canonical_json(normalized[key])}"
            for key in sorted(normalized)
        ) + "}"
    raise ValueError("unsupported projection value")


def compute_projection_digest(projection: dict[str, Any]) -> str:
    """Compute the Rust canonical-json-v1 BLAKE3 projection digest."""
    content = {key: value for key, value in projection.items() if key != "projection_digest"}
    return f"blake3:{blake3(_canonical_json(content).encode('utf-8')).hexdigest()}"


def _observed_projection_digest(projection: Any) -> str:
    try:
        encoded = _canonical_json(projection).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(projection).encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _contains_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "bearer ",
            "authorization:",
            "canary_secret",
            "-----begin private key-----",
        )
    )


def _valid_summary_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= 512 and not _contains_sensitive(value)
    if isinstance(value, list):
        return len(value) <= 64 and all(
            not isinstance(item, (list, dict)) and _valid_summary_value(item) for item in value
        )
    return False


def _validate_projection_section(section: Any, source_revision: int) -> str | None:
    if not isinstance(section, dict):
        return "MALFORMED_SECTION"
    keys = set(section)
    if not _SECTION_REQUIRED.issubset(keys) or keys - (_SECTION_REQUIRED | _SECTION_OPTIONAL):
        return "MALFORMED_SECTION"
    if section["source_revision"] != source_revision:
        return "SOURCE_REVISION_MISMATCH"
    if not isinstance(source_revision, int) or isinstance(source_revision, bool) or source_revision < 0:
        return "MALFORMED_SECTION"
    if not isinstance(section["source_digest"], str) or not _DIGEST.fullmatch(
        section["source_digest"]
    ):
        return "MALFORMED_SECTION"
    state = section["state"]
    if not isinstance(state, str) or not state or len(state) > 128 or _contains_sensitive(state):
        return "REDACTION_FAILED"
    if section["certainty"] not in {"certain", "partial", "uncertain", "unknown"}:
        return "MALFORMED_SECTION"
    freshness = section["freshness"]
    if freshness not in {"fresh", "stale", "expired", "unavailable"}:
        return "MALFORMED_SECTION"
    if not isinstance(section["stale"], bool) or section["stale"] != (freshness != "fresh"):
        return "MALFORMED_SECTION"
    if not isinstance(section["updated_at_ms"], int) or section["updated_at_ms"] <= 0:
        return "MALFORMED_SECTION"
    summary = section["summary"]
    if not isinstance(summary, dict) or len(summary) > 64:
        return "MALFORMED_SECTION"
    for key, value in summary.items():
        if not isinstance(key, str) or not _SUMMARY_KEY.fullmatch(key):
            return "MALFORMED_SECTION"
        if key in _SENSITIVE_KEYS or not _valid_summary_value(value):
            return "REDACTION_FAILED"
    evidence = section["evidence_refs"]
    if (
        not isinstance(evidence, list)
        or len(evidence) > 64
        or len(evidence) != len(set(evidence))
        or any(not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in evidence)
    ):
        return "MALFORMED_SECTION"
    actions = section["permitted_next_actions"]
    if (
        not isinstance(actions, list)
        or len(actions) > 32
        or len(actions) != len(set(actions))
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > 128
            or _contains_sensitive(item)
            for item in actions
        )
    ):
        return "MALFORMED_SECTION"
    error_code = section.get("last_error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code)
    ):
        return "MALFORMED_SECTION"
    return None


def projection_validation_error(projection: Any) -> str | None:
    """Return a stable rejection code, or None when the envelope verifies."""
    if not isinstance(projection, dict):
        return "MALFORMED_PROJECTION"
    keys = set(projection)
    if not _PROJECTION_REQUIRED.issubset(keys) or keys - (
        _PROJECTION_REQUIRED | _PROJECTION_OPTIONAL
    ):
        return "MALFORMED_PROJECTION"
    if projection["contract_version"] != 1 or projection["entity_type"] != (
        "unified_workflow_projection"
    ):
        return "INCOMPATIBLE_PROJECTION"
    if not isinstance(projection["projection_id"], str) or not projection["projection_id"]:
        return "MALFORMED_PROJECTION"
    if (
        not isinstance(projection["projection_revision"], int)
        or isinstance(projection["projection_revision"], bool)
        or projection["projection_revision"] <= 0
    ):
        return "MALFORMED_PROJECTION"
    claimed_digest = projection["projection_digest"]
    if not isinstance(claimed_digest, str) or not _DIGEST.fullmatch(claimed_digest):
        return "MALFORMED_PROJECTION"
    previous = projection.get("previous_projection_digest")
    if previous is not None and (not isinstance(previous, str) or not _DIGEST.fullmatch(previous)):
        return "MALFORMED_PROJECTION"
    if not isinstance(projection["correlation_id"], str) or not projection["correlation_id"]:
        return "MALFORMED_PROJECTION"
    created = projection["created_at_ms"]
    expires = projection["expires_at_ms"]
    if (
        not isinstance(created, int)
        or isinstance(created, bool)
        or created <= 0
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or expires < created
    ):
        return "MALFORMED_PROJECTION"
    revisions = projection["authoritative_source_revisions"]
    if not isinstance(revisions, dict) or set(revisions) != set(_PROJECTION_SECTIONS):
        return "MALFORMED_PROJECTION"
    for name in _PROJECTION_SECTIONS:
        error = _validate_projection_section(projection[name], revisions[name])
        if error is not None:
            return error
    try:
        if compute_projection_digest(projection) != claimed_digest:
            return "DIGEST_MISMATCH"
    except (TypeError, ValueError):
        return "MALFORMED_PROJECTION"
    return None


@dataclass(frozen=True)
class ProjectionConsumerAck:
    contract_version: int
    entity_type: str
    consumer: str
    projection_id: str
    projection_revision: int
    projection_digest: str
    received_at_ms: int
    rendered_at_ms: int
    latency_ms: int
    status: str
    error_code: str | None = None

    @classmethod
    def create(
        cls,
        projection: Any,
        received_at_ms: int,
        rendered_at_ms: int,
        status: str,
        error_code: str | None,
    ) -> "ProjectionConsumerAck":
        projection_id = projection.get("projection_id") if isinstance(projection, dict) else None
        revision = projection.get("projection_revision") if isinstance(projection, dict) else None
        claimed_digest = projection.get("projection_digest") if isinstance(projection, dict) else None
        created_at_ms = projection.get("created_at_ms") if isinstance(projection, dict) else None
        if not isinstance(projection_id, str) or not projection_id:
            projection_id = "invalid-projection"
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            revision = 0
        if not isinstance(claimed_digest, str) or not _DIGEST.fullmatch(claimed_digest):
            claimed_digest = _observed_projection_digest(projection)
        if not isinstance(created_at_ms, int) or created_at_ms <= 0:
            created_at_ms = received_at_ms
        return cls(
            contract_version=1,
            entity_type="projection_consumer_ack",
            consumer="vnpy",
            projection_id=projection_id,
            projection_revision=revision,
            projection_digest=claimed_digest,
            received_at_ms=received_at_ms,
            rendered_at_ms=rendered_at_ms,
            latency_ms=max(0, rendered_at_ms - created_at_ms),
            status=status,
            error_code=error_code,
        )


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
    "tikhub.state": "tikhub",
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
    "model.candidate": "model_candidates",
    "model.audit": "audits",
    "memory.state": "memory",
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
    tikhub: dict[str, Any] = field(default_factory=dict)
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
    model_candidates: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    unified_projection: dict[str, Any] = field(default_factory=dict)
    unified_projection_revision: int = 0
    unified_projection_digest: str | None = None
    unified_source_revisions: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None

    def apply_unified_projection(
        self,
        projection: dict[str, Any],
        rendered_at_ms: int,
    ) -> tuple["ConsoleState", str, str | None]:
        error = projection_validation_error(projection)
        if error is not None:
            return replace(self, last_error=error), "invalid_rejected", error

        incoming_revision = projection["projection_revision"]
        incoming_digest = projection["projection_digest"]
        if incoming_revision < self.unified_projection_revision:
            return replace(self, last_error="STALE_PROJECTION"), "stale_rejected", (
                "STALE_PROJECTION"
            )
        if incoming_revision == self.unified_projection_revision:
            if incoming_digest == self.unified_projection_digest:
                return replace(self, last_error="DUPLICATE_PROJECTION"), "stale_rejected", (
                    "DUPLICATE_PROJECTION"
                )
            return replace(self, last_error="PROJECTION_REVISION_COLLISION"), (
                "invalid_rejected"
            ), "PROJECTION_REVISION_COLLISION"

        if self.unified_projection_revision:
            if incoming_revision != self.unified_projection_revision + 1:
                return replace(self, last_error="OUT_OF_ORDER_PROJECTION"), (
                    "invalid_rejected"
                ), "OUT_OF_ORDER_PROJECTION"
            if projection.get("previous_projection_digest") != self.unified_projection_digest:
                return replace(self, last_error="PROJECTION_CHAIN_MISMATCH"), (
                    "invalid_rejected"
                ), "PROJECTION_CHAIN_MISMATCH"
            previous_sections = self.unified_projection
            for name in _PROJECTION_SECTIONS:
                incoming_source_revision = projection["authoritative_source_revisions"][name]
                previous_source_revision = self.unified_source_revisions[name]
                if incoming_source_revision < previous_source_revision:
                    return replace(self, last_error="STALE_SOURCE_REVISION"), (
                        "stale_rejected"
                    ), "STALE_SOURCE_REVISION"
                if (
                    incoming_source_revision == previous_source_revision
                    and projection[name] != previous_sections[name]
                ):
                    return replace(self, last_error="SOURCE_REVISION_COLLISION"), (
                        "invalid_rejected"
                    ), "SOURCE_REVISION_COLLISION"

        now_ms = max(rendered_at_ms, 1)
        stored = deepcopy(projection)
        return (
            replace(
                self,
                unified_projection=stored,
                unified_projection_revision=incoming_revision,
                unified_projection_digest=incoming_digest,
                unified_source_revisions=dict(projection["authoritative_source_revisions"]),
                revision=self.revision + 1,
                updated_at_ms=now_ms,
                projection_latency_ms=max(0, now_ms - projection["created_at_ms"]),
                correlation_id=projection["correlation_id"],
                last_error=None,
            ),
            "applied",
            None,
        )

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
