"""UI-neutral panel adapter for vn.py's Agent Console."""

from dataclasses import dataclass
from typing import Any

from .models import ConsoleState


@dataclass(frozen=True)
class ConsolePanels:
    mission: dict[str, Any]
    workflow: dict[str, Any]
    wakeups: dict[str, Any]
    routes: dict[str, Any]
    budgets: dict[str, Any]
    capabilities: dict[str, Any]
    mcp: dict[str, Any]
    tikhub: dict[str, Any]
    secret_broker: dict[str, Any]
    qualifications: dict[str, Any]
    grants: dict[str, Any]
    lifecycle: dict[str, Any]
    audits: dict[str, Any]
    artifacts: dict[str, Any]
    evaluation: dict[str, Any]
    health: dict[str, Any]


class AgentConsoleWidget:
    def __init__(self, state: ConsoleState | None = None) -> None:
        self.state = state or ConsoleState()

    def update_state(self, state: ConsoleState) -> None:
        self.state = state

    def panels(self) -> ConsolePanels:
        return ConsolePanels(
            mission=dict(self.state.missions),
            workflow=dict(self.state.workflows),
            wakeups=dict(self.state.wakeups),
            routes=dict(self.state.routes),
            budgets=dict(self.state.budgets),
            capabilities=dict(self.state.capabilities),
            mcp=dict(self.state.mcp),
            tikhub=dict(self.state.tikhub),
            secret_broker=dict(self.state.secret_broker),
            qualifications=dict(self.state.qualifications),
            grants=dict(self.state.grants),
            lifecycle={
                "requests": dict(self.state.lifecycle_requests),
                "results": dict(self.state.lifecycle_results),
            },
            audits=dict(self.state.audits),
            artifacts=dict(self.state.artifacts),
            evaluation={
                "run": dict(self.state.evaluations),
                "budgets": dict(self.state.budgets),
                "score_inputs": dict(self.state.score_inputs),
                "audits": dict(self.state.audits),
                "shadow": dict(self.state.shadow),
                "baseline": dict(self.state.harness),
            },
            health={
                "bridge": self.state.bridge_health,
                "observer_gate": dict(self.state.observer_gate),
                "recovery": dict(self.state.recovery),
                "last_error": self.state.last_error,
            },
        )
