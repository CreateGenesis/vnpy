"""Redacted, revisioned read model for the loopback demonstration UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any
from uuid import UUID

from vnpy.model_production.contracts import canonical_json_v1


_DIGEST = re.compile(r"^(?:sha256|blake3):[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SSE|SZSE|BSE)$")
_INCIDENT = re.compile(r"^[A-Z0-9_]{1,96}$")
_GATEWAYS = frozenset({"XTP", "TORA"})
_ACTIONS = frozenset({"start", "pause", "emergency_stop"})
_FORBIDDEN_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_fingerprint",
        "cancel",
        "cancel_request",
        "credential",
        "credential_ref",
        "main_engine",
        "order",
        "order_request",
        "password",
        "private_key",
        "rpc_endpoint",
        "server_fingerprint",
        "state_store_path",
        "token",
    }
)


@dataclass(frozen=True)
class CandidateProjectionInput:
    candidate_digest: str
    author_lineage_digest: str
    package_digest: str
    readiness: str


@dataclass(frozen=True)
class LatencyProjectionInput:
    count: int
    p50: int
    p95: int
    p99: int
    max: int


@dataclass(frozen=True)
class PositionProjectionInput:
    symbol: str
    quantity: int
    available_quantity: int
    marked_value_minor: int
    unrealized_profit_minor: int
    t_plus_one_locked_quantity: int = 0


@dataclass(frozen=True)
class GatewayProjectionInput:
    gateway: str
    run_digest: str
    state: str
    connection_state: str
    reconciliation_state: str
    net_profit_minor: int
    realized_profit_minor: int
    unrealized_profit_minor: int
    fees_minor: int
    return_bps: int
    max_drawdown_bps: int
    fill_count: int
    positions: tuple[PositionProjectionInput, ...]
    gross_exposure_minor: int
    risk_headroom_minor: int
    local_latency_us: LatencyProjectionInput
    broker_latency_us: LatencyProjectionInput
    incidents: tuple[str, ...]
    residual_exposure_minor: int = 0
    working_order_count: int = 0
    unresolved_outcomes: int = 0
    permitted_next_action: str = "none"


@dataclass(frozen=True)
class HistoricalGatewayInput:
    gateway: str
    net_profit_minor: int
    reconciled: bool
    hard_limit_breaches: int
    unresolved_outcomes: int


@dataclass(frozen=True)
class HistoricalEvidenceInput:
    campaign_digest: str
    candidate_digest: str
    evidence_digest: str
    sessions: tuple[date, ...]
    ready: bool
    gateways: tuple[HistoricalGatewayInput, ...]
    retained_at_ms: int


@dataclass(frozen=True)
class DemoProjectionInput:
    source_revision: int
    updated_at_ms: int
    candidate: CandidateProjectionInput
    campaign_id: str | None
    campaign_digest: str | None
    campaign_state: str
    current_gateways: tuple[GatewayProjectionInput, ...]
    historical_evidence: tuple[HistoricalEvidenceInput, ...]
    risk_state: str
    permitted_actions: tuple[str, ...]


@dataclass(frozen=True)
class DemoProjection:
    revision: int
    source_revision: int
    source_digest: str
    projection_digest: str
    previous_projection_digest: str | None
    updated_at_ms: int
    _canonical_public_json: bytes

    def to_public_dict(self) -> dict[str, Any]:
        """Return a detached browser-safe value."""

        value = json.loads(self._canonical_public_json)
        assert isinstance(value, dict)
        return value


class DemoProjectionStore:
    """Persist the latest valid projection without exposing source objects."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = RLock()

    def current(self) -> DemoProjection | None:
        with self._lock:
            if not self._path.exists():
                return None
            try:
                value = json.loads(self._path.read_bytes())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("PROJECTION_STORE_INVALID") from exc
            if not isinstance(value, dict):
                raise ValueError("PROJECTION_STORE_INVALID")
            return _projection_from_public(value)

    def publish(self, value: DemoProjectionInput) -> DemoProjection:
        _validate_projection_input(value)
        source_payload = _source_payload(value)
        source_digest = _digest(canonical_json_v1(source_payload))
        with self._lock:
            current = self.current()
            if current is not None:
                if value.source_revision < current.source_revision:
                    raise ValueError("PROJECTION_SOURCE_STALE")
                if value.source_revision == current.source_revision:
                    if source_digest == current.source_digest:
                        return current
                    raise ValueError("PROJECTION_SOURCE_REVISION_COLLISION")
            revision = 1 if current is None else current.revision + 1
            previous = None if current is None else current.projection_digest
            public: dict[str, Any] = {
                "contract_version": 1,
                "entity_type": "investor_demo_projection",
                "revision": revision,
                "source_revision": value.source_revision,
                "source_digest": source_digest,
                "projection_digest": "",
                "previous_projection_digest": previous,
                "updated_at_ms": value.updated_at_ms,
                "performance_scope": "broker_simulation",
                **source_payload["view"],
            }
            public["projection_digest"] = _projection_digest(public)
            _assert_no_forbidden_projection(public)
            projection = _projection_from_public(public)
            self._persist(projection._canonical_public_json)
            return projection

    def _persist(self, encoded: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)


def _source_payload(value: DemoProjectionInput) -> dict[str, Any]:
    return {
        "source_revision": value.source_revision,
        "updated_at_ms": value.updated_at_ms,
        "view": {
            "candidate": {
                "candidate_digest": value.candidate.candidate_digest,
                "author_lineage_digest": value.candidate.author_lineage_digest,
                "package_digest": value.candidate.package_digest,
                "readiness": value.candidate.readiness,
            },
            "current": {
                "label": "current_broker_simulation",
                "campaign_id": value.campaign_id,
                "campaign_digest": value.campaign_digest,
                "campaign_state": value.campaign_state,
                "gateways": [_gateway_dict(gateway) for gateway in value.current_gateways],
            },
            "history": [_historical_dict(item) for item in value.historical_evidence],
            "risk_state": value.risk_state,
            "permitted_actions": list(value.permitted_actions),
        },
    }


def _gateway_dict(value: GatewayProjectionInput) -> dict[str, Any]:
    return {
        "gateway": value.gateway,
        "run_digest": value.run_digest,
        "state": value.state,
        "connection_state": value.connection_state,
        "reconciliation_state": value.reconciliation_state,
        "net_profit_minor": value.net_profit_minor,
        "realized_profit_minor": value.realized_profit_minor,
        "unrealized_profit_minor": value.unrealized_profit_minor,
        "fees_minor": value.fees_minor,
        "return_bps": value.return_bps,
        "max_drawdown_bps": value.max_drawdown_bps,
        "fill_count": value.fill_count,
        "positions": [
            {
                "symbol": position.symbol,
                "quantity": position.quantity,
                "available_quantity": position.available_quantity,
                "marked_value_minor": position.marked_value_minor,
                "unrealized_profit_minor": position.unrealized_profit_minor,
                "t_plus_one_locked_quantity": position.t_plus_one_locked_quantity,
            }
            for position in value.positions
        ],
        "gross_exposure_minor": value.gross_exposure_minor,
        "risk_headroom_minor": value.risk_headroom_minor,
        "local_latency_us": _latency_dict(value.local_latency_us),
        "broker_latency_us": _latency_dict(value.broker_latency_us),
        "incidents": list(value.incidents),
        "residual_exposure_minor": value.residual_exposure_minor,
        "working_order_count": value.working_order_count,
        "unresolved_outcomes": value.unresolved_outcomes,
        "permitted_next_action": value.permitted_next_action,
    }


def _historical_dict(value: HistoricalEvidenceInput) -> dict[str, Any]:
    return {
        "label": "historical_broker_simulation_evidence",
        "campaign_digest": value.campaign_digest,
        "candidate_digest": value.candidate_digest,
        "evidence_digest": value.evidence_digest,
        "sessions": [session.isoformat() for session in value.sessions],
        "ready": value.ready,
        "gateways": [
            {
                "gateway": gateway.gateway,
                "net_profit_minor": gateway.net_profit_minor,
                "reconciled": gateway.reconciled,
                "hard_limit_breaches": gateway.hard_limit_breaches,
                "unresolved_outcomes": gateway.unresolved_outcomes,
            }
            for gateway in value.gateways
        ],
        "retained_at_ms": value.retained_at_ms,
    }


def _latency_dict(value: LatencyProjectionInput) -> dict[str, int]:
    return {
        "count": value.count,
        "p50": value.p50,
        "p95": value.p95,
        "p99": value.p99,
        "max": value.max,
    }


def _validate_projection_input(value: DemoProjectionInput) -> None:
    if not _is_positive_int(value.source_revision) or not _is_positive_int(value.updated_at_ms):
        raise ValueError("PROJECTION_REVISION_INVALID")
    candidate = value.candidate
    if any(
        not _DIGEST.fullmatch(item)
        for item in (
            candidate.candidate_digest,
            candidate.author_lineage_digest,
            candidate.package_digest,
        )
    ) or candidate.readiness not in {"ready", "blocked", "active", "unavailable"}:
        raise ValueError("PROJECTION_CANDIDATE_INVALID")
    if (value.campaign_id is None) != (value.campaign_digest is None):
        raise ValueError("PROJECTION_CAMPAIGN_INVALID")
    if value.campaign_id is not None:
        try:
            UUID(value.campaign_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("PROJECTION_CAMPAIGN_INVALID") from exc
        if value.campaign_digest is None or not _DIGEST.fullmatch(value.campaign_digest):
            raise ValueError("PROJECTION_CAMPAIGN_INVALID")
    if not value.campaign_state or len(value.campaign_state) > 64:
        raise ValueError("PROJECTION_CAMPAIGN_INVALID")
    if len(value.current_gateways) > 2 or len({item.gateway for item in value.current_gateways}) != len(
        value.current_gateways
    ):
        raise ValueError("PROJECTION_GATEWAYS_INVALID")
    for gateway in value.current_gateways:
        _validate_gateway(gateway)
    for history in value.historical_evidence:
        _validate_history(history)
    if value.risk_state not in {"normal", "blocking", "blocked", "stopped", "unavailable"}:
        raise ValueError("PROJECTION_RISK_STATE_INVALID")
    if len(set(value.permitted_actions)) != len(value.permitted_actions) or any(
        action not in _ACTIONS for action in value.permitted_actions
    ):
        raise ValueError("PROJECTION_ACTION_INVALID")


def _validate_gateway(value: GatewayProjectionInput) -> None:
    if value.gateway not in _GATEWAYS or not _DIGEST.fullmatch(value.run_digest):
        raise ValueError("PROJECTION_GATEWAY_INVALID")
    if any(
        not item or len(item) > 64
        for item in (value.state, value.connection_state, value.reconciliation_state)
    ):
        raise ValueError("PROJECTION_GATEWAY_STATE_INVALID")
    nonnegative = (
        value.fees_minor,
        value.max_drawdown_bps,
        value.fill_count,
        value.gross_exposure_minor,
        value.risk_headroom_minor,
        value.residual_exposure_minor,
        value.working_order_count,
        value.unresolved_outcomes,
    )
    if any(not _is_nonnegative_int(item) for item in nonnegative):
        raise ValueError("PROJECTION_GATEWAY_METRIC_INVALID")
    if any(
        not _is_int(item)
        for item in (
            value.net_profit_minor,
            value.realized_profit_minor,
            value.unrealized_profit_minor,
            value.return_bps,
        )
    ):
        raise ValueError("PROJECTION_GATEWAY_METRIC_INVALID")
    _validate_latency(value.local_latency_us)
    _validate_latency(value.broker_latency_us)
    if any(_INCIDENT.fullmatch(item) is None for item in value.incidents):
        raise ValueError("PROJECTION_INCIDENT_INVALID")
    if not value.permitted_next_action or len(value.permitted_next_action) > 96:
        raise ValueError("PROJECTION_NEXT_ACTION_INVALID")
    for position in value.positions:
        if _SYMBOL.fullmatch(position.symbol) is None or any(
            not _is_nonnegative_int(item)
            for item in (
                position.quantity,
                position.available_quantity,
                position.marked_value_minor,
                position.t_plus_one_locked_quantity,
            )
        ):
            raise ValueError("PROJECTION_POSITION_INVALID")
        if (
            not _is_int(position.unrealized_profit_minor)
            or position.available_quantity > position.quantity
            or position.t_plus_one_locked_quantity > position.quantity
        ):
            raise ValueError("PROJECTION_POSITION_INVALID")


def _validate_latency(value: LatencyProjectionInput) -> None:
    metrics = (value.count, value.p50, value.p95, value.p99, value.max)
    if any(not _is_nonnegative_int(item) for item in metrics):
        raise ValueError("PROJECTION_LATENCY_INVALID")
    if not value.p50 <= value.p95 <= value.p99 <= value.max:
        raise ValueError("PROJECTION_LATENCY_INVALID")


def _validate_history(value: HistoricalEvidenceInput) -> None:
    if any(
        not _DIGEST.fullmatch(item)
        for item in (value.campaign_digest, value.candidate_digest, value.evidence_digest)
    ):
        raise ValueError("PROJECTION_HISTORY_IDENTITY_INVALID")
    if (
        len(value.sessions) != 5
        or len(set(value.sessions)) != 5
        or tuple(sorted(value.sessions)) != value.sessions
    ):
        raise ValueError("PROJECTION_HISTORY_SESSIONS_INVALID")
    if not _is_positive_int(value.retained_at_ms):
        raise ValueError("PROJECTION_HISTORY_TIMESTAMP_INVALID")
    gateways = {gateway.gateway for gateway in value.gateways}
    if gateways != _GATEWAYS or len(value.gateways) != 2:
        raise ValueError("PROJECTION_HISTORY_GATEWAYS_INVALID")
    for gateway in value.gateways:
        if (
            not isinstance(gateway.reconciled, bool)
            or not _is_int(gateway.net_profit_minor)
            or not _is_nonnegative_int(gateway.hard_limit_breaches)
            or not _is_nonnegative_int(gateway.unresolved_outcomes)
        ):
            raise ValueError("PROJECTION_HISTORY_GATEWAY_INVALID")


def _projection_from_public(value: dict[str, Any]) -> DemoProjection:
    expected = {
        "contract_version",
        "entity_type",
        "revision",
        "source_revision",
        "source_digest",
        "projection_digest",
        "previous_projection_digest",
        "updated_at_ms",
        "performance_scope",
        "candidate",
        "current",
        "history",
        "risk_state",
        "permitted_actions",
    }
    if set(value) != expected:
        raise ValueError("PROJECTION_FIELDS_INVALID")
    if value["contract_version"] != 1 or value["entity_type"] != "investor_demo_projection":
        raise ValueError("PROJECTION_CONTRACT_INVALID")
    if value["performance_scope"] != "broker_simulation":
        raise ValueError("PROJECTION_SCOPE_INVALID")
    if not _is_positive_int(value["revision"]) or not _is_positive_int(value["source_revision"]):
        raise ValueError("PROJECTION_REVISION_INVALID")
    if not _is_positive_int(value["updated_at_ms"]):
        raise ValueError("PROJECTION_TIMESTAMP_INVALID")
    if (
        not _DIGEST.fullmatch(value["source_digest"])
        or value["source_digest"] != _public_source_digest(value)
    ):
        raise ValueError("PROJECTION_SOURCE_DIGEST_INVALID")
    previous = value["previous_projection_digest"]
    if previous is not None and not _DIGEST.fullmatch(previous):
        raise ValueError("PROJECTION_PREVIOUS_DIGEST_INVALID")
    if value["projection_digest"] != _projection_digest(value):
        raise ValueError("PROJECTION_DIGEST_INVALID")
    _validate_public_shape(value)
    _assert_no_forbidden_projection(value)
    encoded = canonical_json_v1(value)
    return DemoProjection(
        revision=value["revision"],
        source_revision=value["source_revision"],
        source_digest=value["source_digest"],
        projection_digest=value["projection_digest"],
        previous_projection_digest=previous,
        updated_at_ms=value["updated_at_ms"],
        _canonical_public_json=encoded,
    )


def _validate_public_shape(value: dict[str, Any]) -> None:
    if set(value["candidate"]) != {
        "candidate_digest",
        "author_lineage_digest",
        "package_digest",
        "readiness",
    }:
        raise ValueError("PROJECTION_CANDIDATE_FIELDS_INVALID")
    if set(value["current"]) != {
        "label",
        "campaign_id",
        "campaign_digest",
        "campaign_state",
        "gateways",
    } or value["current"]["label"] != "current_broker_simulation":
        raise ValueError("PROJECTION_CURRENT_FIELDS_INVALID")
    if not isinstance(value["history"], list) or any(
        not isinstance(item, dict)
        or item.get("label") != "historical_broker_simulation_evidence"
        for item in value["history"]
    ):
        raise ValueError("PROJECTION_HISTORY_FIELDS_INVALID")
    if not isinstance(value["permitted_actions"], list):
        raise ValueError("PROJECTION_ACTIONS_INVALID")


def _assert_no_forbidden_projection(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_KEYS:
                raise ValueError("PROJECTION_REDACTION_FAILED")
            _assert_no_forbidden_projection(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_forbidden_projection(item)


def _projection_digest(value: dict[str, Any]) -> str:
    unsigned = dict(value)
    unsigned.pop("projection_digest", None)
    return _digest(canonical_json_v1(unsigned))


def _public_source_digest(value: dict[str, Any]) -> str:
    source = {
        "source_revision": value["source_revision"],
        "updated_at_ms": value["updated_at_ms"],
        "view": {
            "candidate": value["candidate"],
            "current": value["current"],
            "history": value["history"],
            "risk_state": value["risk_state"],
            "permitted_actions": value["permitted_actions"],
        },
    }
    return _digest(canonical_json_v1(source))


def _digest(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def _is_nonnegative_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return _is_nonnegative_int(value) and value > 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
