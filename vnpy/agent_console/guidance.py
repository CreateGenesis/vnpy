"""Canonical, digest-bound guidance projection read model."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from typing import Any

import blake3


_DIGEST = re.compile(r"^blake3:[0-9a-f]{64}$")
_ENTITY_TYPES = {
    "session", "turn", "draft", "notification", "acknowledgement", "effective_member",
    "auth_binding", "retention", "recovery_checkpoint", "recovery_run", "resource_envelope",
    "agent_allocation", "required_minimum", "resource_decision", "starvation_finding",
    "atomic_action", "safe_boundary", "template", "health",
}
_PAGE_KEYS = {
    "entity_type", "contract_version", "projection_id", "mission_id", "projection_revision",
    "source_revision", "freshness", "certainty", "items", "cursor", "next_cursor",
    "generated_at_ms", "projection_digest",
}
_ITEM_REQUIRED = {
    "entity_type", "entity_id", "entity_revision", "state", "source_digest", "display",
}
_ITEM_KEYS = _ITEM_REQUIRED | {"exact_content", "permitted_actions"}
_RESOURCE_FIELDS = {
    "input_tokens", "output_tokens", "model_calls", "tool_calls", "cli_calls",
    "subagent_dispatches", "wall_time_ms", "cost_microunits",
}
_SUMMARY_KEYS = {
    "entity_type", "contract_version", "mission_id", "source_revision", "session_counts",
    "notification_counts", "effective_guidance_revision", "effective_guidance_digest",
    "oldest_queue_age_ms", "auth", "recovery", "retention", "resources", "starvation",
    "health", "last_error_code", "permitted_actions", "summary_digest",
}
_SENSITIVE_KEYS = {
    "api_key", "access_key", "authorization", "password", "secret", "secret_value",
    "token", "cookie", "private_key", "raw_body", "raw_header", "raw_headers", "prompt",
}


def _is_uint(value: Any, *, minimum: int = 0) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return f"blake3:{blake3.blake3(_canonical_json(value)).hexdigest()}"


def _validate_actions(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def _validate_item(item: Any) -> None:
    if not isinstance(item, dict) or not _ITEM_REQUIRED <= item.keys() <= _ITEM_KEYS:
        raise ValueError("invalid guidance projection item")
    if (
        item["entity_type"] not in _ENTITY_TYPES
        or not isinstance(item["entity_id"], str)
        or not item["entity_id"]
        or not _is_uint(item["entity_revision"])
        or not isinstance(item["state"], str)
        or not item["state"]
        or not isinstance(item["source_digest"], str)
        or _DIGEST.fullmatch(item["source_digest"]) is None
        or not isinstance(item["display"], dict)
        or ("permitted_actions" in item and not _validate_actions(item["permitted_actions"]))
    ):
        raise ValueError("invalid guidance projection item")


def _validate_page(page: Any, *, allow_unsealed: bool = False) -> None:
    if not isinstance(page, dict) or set(page) != _PAGE_KEYS:
        raise ValueError("invalid guidance projection page")
    digest = page["projection_digest"]
    if (
        page["entity_type"] != "guidance_projection_page"
        or page["contract_version"] != 1
        or not isinstance(page["projection_id"], str)
        or not page["projection_id"]
        or not isinstance(page["mission_id"], str)
        or not page["mission_id"]
        or not _is_uint(page["projection_revision"], minimum=1)
        or not _is_uint(page["source_revision"])
        or page["freshness"] not in {"fresh", "stale", "expired", "unknown"}
        or page["certainty"] not in {"known", "uncertain", "reconciliation_required"}
        or not isinstance(page["items"], list)
        or len(page["items"]) > 100
        or not (page["cursor"] is None or isinstance(page["cursor"], str))
        or not (page["next_cursor"] is None or isinstance(page["next_cursor"], str))
        or not _is_uint(page["generated_at_ms"])
        or not isinstance(digest, str)
        or (digest == "" and not allow_unsealed)
        or (digest != "" and _DIGEST.fullmatch(digest) is None)
    ):
        raise ValueError("invalid guidance projection page")
    for item in page["items"]:
        _validate_item(item)
    if _contains_sensitive_data(page):
        raise ValueError("secret-bearing guidance projection")


def _ordered_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    by_cursor: dict[str | None, dict[str, Any]] = {}
    for page in pages:
        cursor = page["cursor"]
        if cursor in by_cursor:
            raise ValueError("duplicate guidance projection cursor")
        by_cursor[cursor] = page
    current = by_cursor.get(None)
    if current is None:
        return None
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    while True:
        identity = id(current)
        if identity in seen:
            raise ValueError("cyclic guidance projection cursor chain")
        seen.add(identity)
        ordered.append(current)
        next_cursor = current["next_cursor"]
        if next_cursor is None:
            break
        current = by_cursor.get(next_cursor)
        if current is None:
            return None
    if len(ordered) != len(pages):
        raise ValueError("disconnected guidance projection cursor chain")
    return ordered


def _validate_page_set_metadata(pages: list[dict[str, Any]]) -> None:
    first = pages[0]
    fixed = (
        "projection_id", "mission_id", "projection_revision", "source_revision", "freshness",
        "certainty", "generated_at_ms", "projection_digest",
    )
    if any(any(page[name] != first[name] for name in fixed) for page in pages[1:]):
        raise ValueError("inconsistent guidance projection page set")


def _projection_page_set_digest(pages: list[dict[str, Any]]) -> str:
    material: list[dict[str, Any]] = []
    for page in pages:
        value = deepcopy(page)
        value.pop("projection_digest", None)
        material.append(value)
    return _digest({"domain": "guidance-projection-page-set.v1", "pages": material})


def seal_projection_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and seal one complete cursor chain with a shared page-set digest."""
    if not pages:
        raise ValueError("guidance projection page set is empty")
    copies = deepcopy(pages)
    for page in copies:
        _validate_page(page, allow_unsealed=True)
    ordered = _ordered_pages(copies)
    if ordered is None:
        raise ValueError("incomplete guidance projection cursor chain")
    _validate_page_set_metadata(ordered)
    digest = _projection_page_set_digest(ordered)
    for page in ordered:
        page["projection_digest"] = digest
    return ordered


def _ack_digest(ack: dict[str, Any]) -> str:
    value = deepcopy(ack)
    value.pop("ack_digest", None)
    return _digest({"domain": "guidance-projection-consumer-ack.v1", "ack": value})


def validate_consumer_ack(ack: dict[str, Any]) -> None:
    required = {
        "entity_type", "contract_version", "consumer", "projection_id", "projection_revision",
        "projection_digest", "applied_at_ms", "ack_digest",
    }
    if (
        not isinstance(ack, dict)
        or set(ack) != required
        or ack["entity_type"] != "guidance_projection_consumer_ack"
        or ack["contract_version"] != 1
        or ack["consumer"] not in {"vnpy", "master"}
        or not isinstance(ack["projection_id"], str)
        or not ack["projection_id"]
        or not _is_uint(ack["projection_revision"], minimum=1)
        or not isinstance(ack["projection_digest"], str)
        or _DIGEST.fullmatch(ack["projection_digest"]) is None
        or not _is_uint(ack["applied_at_ms"])
        or not isinstance(ack["ack_digest"], str)
        or _DIGEST.fullmatch(ack["ack_digest"]) is None
    ):
        raise ValueError("invalid guidance consumer acknowledgement")
    if _ack_digest(ack) != ack["ack_digest"]:
        raise ValueError("guidance consumer acknowledgement digest mismatch")


def _validate_resource_vector(vector: Any) -> None:
    if (
        not isinstance(vector, dict)
        or set(vector) != _RESOURCE_FIELDS
        or not all(_is_uint(value) for value in vector.values())
    ):
        raise ValueError("invalid guidance resource vector")


def _summary_digest(summary: dict[str, Any]) -> str:
    value = deepcopy(summary)
    value.pop("summary_digest", None)
    return _digest({"domain": "unified-guidance-summary.v1", "summary": value})


def _validate_summary(summary: Any, *, allow_unsealed: bool = False) -> None:
    if not isinstance(summary, dict) or set(summary) != _SUMMARY_KEYS:
        raise ValueError("invalid unified guidance summary")
    digest = summary["summary_digest"]
    if (
        summary["entity_type"] != "unified_guidance_summary"
        or summary["contract_version"] != 1
        or not isinstance(summary["mission_id"], str)
        or not summary["mission_id"]
        or not _is_uint(summary["source_revision"])
        or not _is_uint(summary["effective_guidance_revision"])
        or not isinstance(summary["effective_guidance_digest"], str)
        or _DIGEST.fullmatch(summary["effective_guidance_digest"]) is None
        or not _is_uint(summary["oldest_queue_age_ms"])
        or summary["health"] not in {"ready", "degraded", "blocked", "disabled", "recovering"}
        or not (summary["last_error_code"] is None or isinstance(summary["last_error_code"], str))
        or not _validate_actions(summary["permitted_actions"])
        or not isinstance(digest, str)
        or (digest == "" and not allow_unsealed)
        or (digest != "" and _DIGEST.fullmatch(digest) is None)
    ):
        raise ValueError("invalid unified guidance summary")
    for counts_name in ("session_counts", "notification_counts"):
        counts = summary[counts_name]
        if not isinstance(counts, dict) or not all(
            isinstance(key, str) and key and _is_uint(value) for key, value in counts.items()
        ):
            raise ValueError("invalid unified guidance summary counts")
    auth = summary["auth"]
    if (
        not isinstance(auth, dict)
        or set(auth) != {"auth_session_id", "state", "verification_epoch", "expires_at_ms"}
        or not (auth["auth_session_id"] is None or isinstance(auth["auth_session_id"], str))
        or auth["state"] not in {"verifying", "active", "revoked", "expired", "unavailable"}
        or not _is_uint(auth["verification_epoch"])
        or not (auth["expires_at_ms"] is None or _is_uint(auth["expires_at_ms"]))
    ):
        raise ValueError("invalid unified guidance auth summary")
    recovery = summary["recovery"]
    if (
        not isinstance(recovery, dict)
        or set(recovery) != {
            "state", "checkpoint_age_ms", "state_visible_elapsed_ms", "resume_elapsed_ms",
        }
        or recovery["state"] not in {
            "ready", "starting", "state_visible", "resuming", "completed", "actionably_blocked",
        }
        or not _is_uint(recovery["checkpoint_age_ms"])
        or not (
            recovery["state_visible_elapsed_ms"] is None
            or _is_uint(recovery["state_visible_elapsed_ms"])
        )
        or not (recovery["resume_elapsed_ms"] is None or _is_uint(recovery["resume_elapsed_ms"]))
    ):
        raise ValueError("invalid unified guidance recovery summary")
    retention = summary["retention"]
    retention_keys = {
        "waiting_count", "retaining_count", "eligible_count", "blocked_count",
        "next_delete_after_ms",
    }
    if (
        not isinstance(retention, dict)
        or set(retention) != retention_keys
        or not all(_is_uint(retention[key]) for key in retention_keys - {"next_delete_after_ms"})
        or not (
            retention["next_delete_after_ms"] is None
            or _is_uint(retention["next_delete_after_ms"])
        )
    ):
        raise ValueError("invalid unified guidance retention summary")
    resources = summary["resources"]
    vector_names = {
        "ceiling", "allocated", "protected", "reserved", "consumed", "remaining", "burn_rate",
        "projected_usage",
    }
    if (
        not isinstance(resources, dict)
        or set(resources) != vector_names | {"state", "forecast_horizon_ms"}
        or resources["state"] not in {"healthy", "conserving", "deficit", "exhausted", "uncertain"}
        or not _is_uint(resources["forecast_horizon_ms"], minimum=1)
    ):
        raise ValueError("invalid unified guidance resource summary")
    for name in vector_names:
        _validate_resource_vector(resources[name])
    starvation = summary["starvation"]
    starvation_keys = {"open_warning_count", "open_blocking_count", "blocked_operation_count"}
    if (
        not isinstance(starvation, dict)
        or set(starvation) != starvation_keys
        or not all(_is_uint(starvation[key]) for key in starvation_keys)
    ):
        raise ValueError("invalid unified guidance starvation summary")
    if _contains_sensitive_data(summary):
        raise ValueError("secret-bearing unified guidance summary")


def seal_unified_guidance_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Seal a canonical redacted unified summary."""
    value = deepcopy(summary)
    _validate_summary(value, allow_unsealed=True)
    value["summary_digest"] = _summary_digest(value)
    _validate_summary(value)
    return value


@dataclass
class GuidanceViewState:
    """Last-known-valid monotonic projection assembled from canonical cursor pages."""

    projection_id: str = ""
    mission_id: str = ""
    projection_revision: int = 0
    source_revision: int = 0
    projection_digest: str = ""
    pages: dict[str | None, dict[str, Any]] = field(default_factory=dict)
    entities: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    _pending_revision: int | None = field(default=None, init=False, repr=False)
    _pending_projection_id: str = field(default="", init=False, repr=False)
    _pending_mission_id: str = field(default="", init=False, repr=False)
    _pending_digest: str = field(default="", init=False, repr=False)
    _pending_update_kind: str = field(default="", init=False, repr=False)
    _pending_pages: dict[str | None, dict[str, Any]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @property
    def revision(self) -> int:
        """Compatibility alias for callers migrating to the canonical field name."""
        return self.projection_revision

    @property
    def guidance(self) -> list[dict[str, Any]]:
        guidance_types = {
            "session", "turn", "draft", "notification", "acknowledgement", "effective_member",
        }
        return [
            deepcopy(item)
            for (entity_type, _), item in self.entities.items()
            if entity_type in guidance_types
        ]

    @property
    def effective_documents(self) -> list[dict[str, Any]]:
        """Return effective members in deterministic revision and position order."""
        members = [
            deepcopy(item)
            for (entity_type, _), item in self.entities.items()
            if entity_type == "effective_member"
        ]
        members.sort(
            key=lambda item: (
                item.get("entity_revision", 0),
                item.get("display", {}).get("position", 0)
                if isinstance(item.get("display"), dict)
                else 0,
                item.get("entity_id", ""),
            )
        )
        return members

    @property
    def unresolved_conflicts(self) -> list[dict[str, Any]]:
        """Return only durable deferred or clarification-required guidance outcomes."""
        unresolved_states = {"deferred", "clarification_required", "conflict"}
        return [
            deepcopy(item)
            for (entity_type, _), item in self.entities.items()
            if entity_type in {"notification", "acknowledgement"}
            and item.get("state") in unresolved_states
        ]

    def cancellation_eligibility(self, notification_id: str, *, now_ms: int) -> str:
        """Classify projected cancellation without granting cancellation authority."""
        if not notification_id or not _is_uint(now_ms):
            raise ValueError("invalid cancellation query")
        item = self.entities.get(("notification", notification_id))
        if item is None:
            return "not_found"
        if item.get("state") != "pending" or "cancel" not in item.get("permitted_actions", []):
            return "too_late"
        display = item.get("display")
        expires_at_ms = display.get("expires_at_ms") if isinstance(display, dict) else None
        if isinstance(expires_at_ms, int) and not isinstance(expires_at_ms, bool) and expires_at_ms <= now_ms:
            return "expired"
        return "eligible"

    def _clear_pending(self) -> None:
        self._pending_revision = None
        self._pending_projection_id = ""
        self._pending_mission_id = ""
        self._pending_digest = ""
        self._pending_update_kind = ""
        self._pending_pages = {}

    def apply(
        self,
        projection: dict[str, Any],
        *,
        update_kind: str | None = None,
    ) -> str:
        _validate_page(projection)
        resolved_kind = update_kind or (
            "snapshot" if self.projection_revision == 0 else "delta"
        )
        if resolved_kind not in {"snapshot", "delta"}:
            raise ValueError("invalid guidance projection update kind")
        revision = projection["projection_revision"]
        if self.projection_id and (
            projection["projection_id"] != self.projection_id
            or projection["mission_id"] != self.mission_id
        ):
            raise ValueError("guidance projection identity changed")
        if revision < self.projection_revision:
            return "stale"
        if revision == self.projection_revision and self.projection_digest:
            current = self.pages.get(projection["cursor"])
            return "duplicate" if current == projection else "stale"
        if self._pending_revision is not None and revision < self._pending_revision:
            return "stale"
        if self._pending_revision != revision:
            self._pending_revision = revision
            self._pending_projection_id = projection["projection_id"]
            self._pending_mission_id = projection["mission_id"]
            self._pending_digest = projection["projection_digest"]
            self._pending_update_kind = resolved_kind
            self._pending_pages = {}
        elif (
            projection["projection_id"] != self._pending_projection_id
            or projection["mission_id"] != self._pending_mission_id
            or projection["projection_digest"] != self._pending_digest
            or resolved_kind != self._pending_update_kind
        ):
            return "stale"
        cursor = projection["cursor"]
        existing = self._pending_pages.get(cursor)
        if existing is not None:
            return "duplicate" if existing == projection else "stale"
        self._pending_pages[cursor] = deepcopy(projection)
        ordered = _ordered_pages(list(self._pending_pages.values()))
        if ordered is None:
            return "partial"
        _validate_page_set_metadata(ordered)
        if _projection_page_set_digest(ordered) != self._pending_digest:
            self._clear_pending()
            raise ValueError("guidance projection page-set digest mismatch")
        if self.projection_revision and ordered[0]["source_revision"] <= self.source_revision:
            self._clear_pending()
            return "stale"

        next_entities = (
            {} if self._pending_update_kind == "snapshot" else deepcopy(self.entities)
        )
        for page in ordered:
            for candidate in page["items"]:
                key = (candidate["entity_type"], candidate["entity_id"])
                previous = next_entities.get(key)
                if previous is not None and (
                    candidate["entity_revision"] < previous["entity_revision"]
                    or (
                        candidate["entity_revision"] == previous["entity_revision"]
                        and candidate != previous
                    )
                ):
                    self._clear_pending()
                    return "stale"
                next_entities[key] = deepcopy(candidate)

        first = ordered[0]
        self.projection_id = first["projection_id"]
        self.mission_id = first["mission_id"]
        self.projection_revision = first["projection_revision"]
        self.source_revision = first["source_revision"]
        self.projection_digest = first["projection_digest"]
        self.pages = {page["cursor"]: deepcopy(page) for page in ordered}
        self.entities = next_entities
        self._clear_pending()
        return "applied"

    def apply_summary(self, summary: dict[str, Any]) -> str:
        _validate_summary(summary)
        if _summary_digest(summary) != summary["summary_digest"]:
            raise ValueError("unified guidance summary digest mismatch")
        if self.mission_id and summary["mission_id"] != self.mission_id:
            raise ValueError("unified guidance summary mission changed")
        previous_revision = self.summary.get("source_revision", -1)
        if summary["source_revision"] < previous_revision:
            return "stale"
        if summary["source_revision"] == previous_revision:
            return "duplicate" if summary == self.summary else "stale"
        self.summary = deepcopy(summary)
        return "applied"

    def consumer_ack(self, consumer: str, *, applied_at_ms: int) -> dict[str, Any]:
        if consumer not in {"vnpy", "master"} or self.projection_revision <= 0 or not _is_uint(applied_at_ms):
            raise ValueError("invalid guidance consumer acknowledgement")
        ack = {
            "entity_type": "guidance_projection_consumer_ack",
            "contract_version": 1,
            "consumer": consumer,
            "projection_id": self.projection_id,
            "projection_revision": self.projection_revision,
            "projection_digest": self.projection_digest,
            "applied_at_ms": applied_at_ms,
            "ack_digest": "",
        }
        ack["ack_digest"] = _ack_digest(ack)
        validate_consumer_ack(ack)
        return ack

    def redacted_summary(self) -> dict[str, Any]:
        return deepcopy(self.summary)


def _contains_sensitive_data(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _SENSITIVE_KEYS or _contains_sensitive_data(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_data(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return (
            "bearer " in lowered
            or "authorization:" in lowered
            or "canary_secret" in lowered
            or "-----begin private key-----" in lowered
        )
    return False
