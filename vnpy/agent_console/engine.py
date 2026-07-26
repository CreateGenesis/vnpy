"""Agent Console projection engine with last-known-state semantics."""

from collections import deque
from copy import deepcopy
from threading import Lock
from time import time_ns
from typing import Any

from vnpy.agent_bridge.events import AgentEvent
from vnpy.trader.engine import BaseEngine

from .models import ConsoleState, ProjectionConsumerAck
from .mcp import MCP_EVENT_TYPES, McpViewState
from .qualification import QUALIFICATION_EVENT_TYPES, QualificationViewState
from .tikhub import TIKHUB_EVENT_TYPES, TikHubViewState
from .guidance import GuidanceViewState


class AgentConsoleEngine(BaseEngine):
    def __init__(self, main_engine: Any | None = None, event_engine: Any | None = None) -> None:
        if (main_engine is None) != (event_engine is None):
            raise ValueError("main_engine and event_engine must be supplied together")
        if main_engine is not None and event_engine is not None:
            super().__init__(main_engine, event_engine, "agent_console")
        else:
            self.main_engine = None
            self.event_engine = None
            self.engine_name = "agent_console"
        self._state = ConsoleState()
        self._mcp_state = McpViewState()
        self._qualification_state = QualificationViewState()
        self._tikhub_state = TikHubViewState()
        self._guidance_state = GuidanceViewState()
        self._guidance_acks: deque[dict[str, Any]] = deque()
        self._guidance_drafts: dict[tuple[str, str], dict[str, Any]] = {}
        self._guidance_session_provider: Any | None = None
        self._guidance_reconnect_required = False
        self._guidance_rebuild_snapshot = False
        self._projection_acks: deque[ProjectionConsumerAck] = deque()
        self._lock = Lock()

    @property
    def guidance_state(self) -> GuidanceViewState:
        with self._lock:
            return self._guidance_state

    def apply_guidance_projection(
        self,
        projection: dict[str, Any],
        *,
        applied_at_ms: int | None = None,
    ) -> str:
        with self._lock:
            applied_at = applied_at_ms if applied_at_ms is not None else time_ns() // 1_000_000
            return self._apply_guidance_projection_locked(projection, applied_at)

    def _apply_guidance_projection_locked(
        self,
        projection: dict[str, Any],
        applied_at_ms: int,
    ) -> str:
        self._verify_guidance_session_locked(applied_at_ms)
        update_kind = "snapshot" if self._guidance_rebuild_snapshot else None
        status = self._guidance_state.apply(projection, update_kind=update_kind)
        if status == "applied":
            self._guidance_acks.append(
                self._guidance_state.consumer_ack("vnpy", applied_at_ms=applied_at_ms)
            )
            self._guidance_rebuild_snapshot = False
        return status

    def next_guidance_projection_ack(self) -> dict[str, Any] | None:
        """Return the next durable-projection ACK exactly once."""
        with self._lock:
            if not self._guidance_acks:
                return None
            return self._guidance_acks.popleft()

    def mark_guidance_disconnected(self) -> None:
        """Require a fresh host-session check before rebuilding scoped guidance."""
        with self._lock:
            self._guidance_reconnect_required = True
            self._guidance_rebuild_snapshot = False

    def begin_guidance_reconnect(
        self,
        session_provider: Any,
        *,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        """Reverify the host login and request an authoritative snapshot."""
        checked_at_ms = now_ms if now_ms is not None else time_ns() // 1_000_000
        if isinstance(checked_at_ms, bool) or checked_at_ms < 0:
            raise ValueError("invalid reconnect timestamp")
        with self._lock:
            self._guidance_session_provider = session_provider
            self._guidance_reconnect_required = True
            self._verify_guidance_session_locked(checked_at_ms)
            self._guidance_rebuild_snapshot = True
            return {
                "auth_session_id": str(session_provider.auth_session_id),
                "operator_id": str(session_provider.operator_id),
                "last_projection_id": self._guidance_state.projection_id,
                "last_projection_revision": self._guidance_state.projection_revision,
                "last_projection_digest": self._guidance_state.projection_digest,
                "requires_authoritative_snapshot": True,
                "isolated_draft_count": len(self._guidance_drafts),
            }

    def _verify_guidance_session_locked(self, now_ms: int) -> None:
        provider = self._guidance_session_provider
        if provider is None:
            if self._guidance_reconnect_required:
                raise PermissionError("operating-system session re-verification required")
            return
        state = provider.refresh(now_ms)
        state_value = getattr(state, "value", state)
        if state_value != "verified":
            self._guidance_reconnect_required = True
            self._guidance_rebuild_snapshot = False
            raise PermissionError("operating-system session could not be reverified")
        self._guidance_reconnect_required = False

    def checkpoint_guidance_draft(
        self,
        mission_id: str,
        session_id: str,
        *,
        revision: int,
        content: Any,
        checkpointed_at_ms: int,
    ) -> str:
        """Checkpoint unsent dynamic content outside the authoritative projection."""
        if (
            not mission_id
            or not session_id
            or isinstance(revision, bool)
            or revision < 0
            or isinstance(checkpointed_at_ms, bool)
            or checkpointed_at_ms < 0
        ):
            raise ValueError("invalid guidance draft checkpoint")
        key = (mission_id, session_id)
        candidate = {
            "mission_id": mission_id,
            "session_id": session_id,
            "revision": revision,
            "content": deepcopy(content),
            "checkpointed_at_ms": checkpointed_at_ms,
        }
        with self._lock:
            previous = self._guidance_drafts.get(key)
            if previous is not None:
                if revision < previous["revision"]:
                    return "stale"
                if revision == previous["revision"]:
                    if candidate == previous:
                        return "duplicate"
                    raise ValueError("guidance draft revision collision")
            self._guidance_drafts[key] = candidate
            return "applied"

    def guidance_draft(self, mission_id: str, session_id: str) -> dict[str, Any] | None:
        """Read one isolated local draft without projecting it to the Master."""
        with self._lock:
            value = self._guidance_drafts.get((mission_id, session_id))
            return deepcopy(value) if value is not None else None

    def close(self) -> None:
        """Release only console-owned resources; trading engines remain untouched."""
        return

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
            if event.event_type in {"guidance.projection", "guidance_projection_page"}:
                now_ms = time_ns() // 1_000_000
                self._apply_guidance_projection_locked(event.payload, now_ms)
                return self._state
            if event.event_type in {"guidance.summary", "unified_guidance_summary"}:
                self._verify_guidance_session_locked(time_ns() // 1_000_000)
                self._guidance_state.apply_summary(event.payload)
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
