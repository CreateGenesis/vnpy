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

TikHubAction = Literal[
    "disable_global",
    "disable_entry",
    "cancel_mission",
    "refresh_status",
    "get_evidence",
]


def research_control(action: ResearchAction, target_id: str) -> AgentEvent:
    if not target_id:
        raise ValueError("research control target is required")
    return AgentEvent(
        event_type="research.control",
        payload={"action": action, "target_id": target_id, "contract_version": 1},
        priority=EventPriority.CRITICAL,
    )


def tikhub_control(action: TikHubAction, target_id: str) -> AgentEvent:
    if not target_id:
        raise ValueError("TikHub control target is required")
    if action not in {
        "disable_global",
        "disable_entry",
        "cancel_mission",
        "refresh_status",
        "get_evidence",
    }:
        raise ValueError("unsupported TikHub control")
    return AgentEvent(
        event_type="tikhub.control",
        payload={"action": action, "target_id": target_id, "contract_version": 1},
        priority=(
            EventPriority.CRITICAL
            if action in {"disable_global", "disable_entry", "cancel_mission"}
            else EventPriority.ROUTINE
        ),
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
