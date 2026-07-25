"""Agent Console projection engine with last-known-state semantics."""

from collections import deque
from threading import Lock
from time import time_ns
from typing import Any

from vnpy.agent_bridge.events import AgentEvent

from .models import ConsoleState, ProjectionConsumerAck
from .mcp import MCP_EVENT_TYPES, McpViewState
from .qualification import QUALIFICATION_EVENT_TYPES, QualificationViewState
from .tikhub import TIKHUB_EVENT_TYPES, TikHubViewState


class AgentConsoleEngine:
    def __init__(self) -> None:
        self._state = ConsoleState()
        self._mcp_state = McpViewState()
        self._qualification_state = QualificationViewState()
        self._tikhub_state = TikHubViewState()
        self._projection_acks: deque[ProjectionConsumerAck] = deque()
        self._lock = Lock()

    @property
    def state(self) -> ConsoleState:
        with self._lock:
            return self._state

    @property
    def qualification_state(self) -> QualificationViewState:
        with self._lock:
            return self._qualification_state

    @property
    def mcp_state(self) -> McpViewState:
        with self._lock:
            return self._mcp_state

    @property
    def tikhub_state(self) -> TikHubViewState:
        with self._lock:
            return self._tikhub_state

    def apply_projection(
        self,
        projection: dict[str, Any],
        *,
        received_at_ms: int | None = None,
        rendered_at_ms: int | None = None,
    ) -> ProjectionConsumerAck:
        """Apply one exact unified projection and queue its convergence acknowledgement."""
        received = received_at_ms if received_at_ms is not None else time_ns() // 1_000_000
        if received <= 0:
            received = 1
        with self._lock:
            rendered = rendered_at_ms if rendered_at_ms is not None else time_ns() // 1_000_000
            rendered = max(rendered, received)
            return self._apply_projection_locked(projection, received, rendered)

    def _apply_projection_locked(
        self,
        projection: dict[str, Any],
        received_at_ms: int,
        rendered_at_ms: int,
    ) -> ProjectionConsumerAck:
        self._state, status, error_code = self._state.apply_unified_projection(
            projection,
            rendered_at_ms,
        )
        ack = ProjectionConsumerAck.create(
            projection,
            received_at_ms,
            rendered_at_ms,
            status,
            error_code,
        )
        self._projection_acks.append(ack)
        return ack

    def next_projection_ack(self) -> ProjectionConsumerAck | None:
        with self._lock:
            if not self._projection_acks:
                return None
            return self._projection_acks.popleft()

    def apply(self, event: AgentEvent) -> ConsoleState:
        with self._lock:
            if event.event_type in {"workflow.projection", "unified_workflow_projection"}:
                now_ms = time_ns() // 1_000_000
                self._apply_projection_locked(event.payload, now_ms, now_ms)
                return self._state
            if event.event_type in TIKHUB_EVENT_TYPES:
                self._tikhub_state = self._tikhub_state.apply(
                    event.event_type,
                    event.payload,
                    event.correlation_id,
                    event.contract_version,
                )
                self._state = self._state.apply(
                    "tikhub.state",
                    self._tikhub_state.console_payload(),
                    event.correlation_id,
                    event.event_time_ms,
                )
                return self._state
            if event.event_type in MCP_EVENT_TYPES:
                self._mcp_state = self._mcp_state.apply(
                    event.event_type,
                    event.payload,
                    event.correlation_id,
                    event.event_time_ms,
                )
                field_event = (
                    "secret_broker.state"
                    if event.event_type == "secret_broker.state"
                    else "mcp.state"
                )
                projection = (
                    dict(self._mcp_state.secret_broker)
                    if field_event == "secret_broker.state"
                    else self._mcp_state.console_payload()
                )
                projection["revision"] = self._mcp_state.revision
                self._state = self._state.apply(
                    field_event,
                    projection,
                    event.correlation_id,
                    event.event_time_ms,
                )
                return self._state
            if event.event_type in QUALIFICATION_EVENT_TYPES:
                self._qualification_state = self._qualification_state.apply(
                    event.event_type,
                    event.payload,
                    event.correlation_id,
                    event.event_time_ms,
                )
                if event.event_type not in {"qualification.state", "grant.state"}:
                    return self._state
            self._state = self._state.apply(
                event.event_type,
                event.payload,
                event.correlation_id,
                event.event_time_ms,
            )
            return self._state
