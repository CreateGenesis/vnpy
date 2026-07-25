"""Read-only lifecycle, gray, reconciliation, incident, and rollback projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelLifecycleViewState:
    revision: int = 0
    package_digest: str = ""
    stage: str = "stopped"
    gate_states: dict[str, str] = field(default_factory=dict)
    gray_remaining: dict[str, int] = field(default_factory=dict)
    broker_outcomes: dict[str, int] = field(default_factory=dict)
    reconciliation_state: str = "unknown"
    incidents: tuple[str, ...] = ()
    emergency_stop: bool = False
    rollback_state: str | None = None
    permitted_next_actions: tuple[str, ...] = ()
    last_request_id: str | None = None
    last_decision_status: str | None = None
    stop_state: str | None = None

    @classmethod
    def from_projection(cls, payload: dict[str, Any]) -> ModelLifecycleViewState:
        if payload.get("contract_version") != 2 or payload.get("entity_type") != "model_lifecycle_projection":
            raise ValueError("incompatible lifecycle projection")
        forbidden = {"api_key", "secret", "send_order", "cancel_order", "clear_breaker"}
        if forbidden.intersection(payload):
            raise ValueError("lifecycle projection exposes forbidden control")
        return cls(
            revision=int(payload["revision"]),
            package_digest=str(payload["package_digest"]),
            stage=str(payload["stage"]),
            gate_states=dict(payload.get("gate_states", {})),
            gray_remaining=dict(payload.get("gray_remaining", {})),
            broker_outcomes=dict(payload.get("broker_outcomes", {})),
            reconciliation_state=str(payload.get("reconciliation_state", "unknown")),
            incidents=tuple(payload.get("incidents", ())),
            emergency_stop=bool(payload.get("emergency_stop", False)),
            rollback_state=payload.get("rollback_state"),
            permitted_next_actions=tuple(payload.get("permitted_next_actions", ())),
            last_request_id=payload.get("last_request_id"),
            last_decision_status=payload.get("last_decision_status"),
            stop_state=payload.get("stop_state"),
        )
