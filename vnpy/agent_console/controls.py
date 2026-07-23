"""Versioned research controls published only through the Agent bridge."""

from dataclasses import dataclass
from typing import Literal

from vnpy.agent_bridge.autonomous_control import LifecycleRequest, lifecycle_request_event
from vnpy.agent_bridge.engine import AgentBridgeEngine
from vnpy.agent_bridge.events import AgentEvent, EventPriority


ResearchAction = Literal[
    "create",
    "pause",
    "resume",
    "retry",
    "cancel",
    "revoke_qualification",
    "requalify",
    "disable_capability",
    "revoke_capability",
    "unload_skill",
    "stop_evaluation",
    "disable_bridge",
]


def research_control(action: ResearchAction, target_id: str) -> AgentEvent:
    if not target_id:
        raise ValueError("research control target is required")
    return AgentEvent(
        event_type="research.control",
        payload={"action": action, "target_id": target_id, "contract_version": 1},
        priority=EventPriority.CRITICAL,
    )


@dataclass
class ResearchControlPublisher:
    bridge: AgentBridgeEngine

    def publish(self, action: ResearchAction, target_id: str) -> int:
        return self.bridge.publish_observation(research_control(action, target_id))

    def publish_lifecycle_request(
        self, request: LifecycleRequest, *, now_ms: int | None = None
    ) -> int:
        return self.bridge.publish_observation(lifecycle_request_event(request, now_ms=now_ms))
