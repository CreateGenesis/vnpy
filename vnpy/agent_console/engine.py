"""Agent Console projection engine with last-known-state semantics."""

from threading import Lock

from vnpy.agent_bridge.events import AgentEvent

from .models import ConsoleState
from .mcp import MCP_EVENT_TYPES, McpViewState
from .qualification import QUALIFICATION_EVENT_TYPES, QualificationViewState


class AgentConsoleEngine:
    def __init__(self) -> None:
        self._state = ConsoleState()
        self._mcp_state = McpViewState()
        self._qualification_state = QualificationViewState()
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

    def apply(self, event: AgentEvent) -> ConsoleState:
        with self._lock:
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
