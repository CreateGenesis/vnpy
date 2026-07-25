"""Versioned research controls published only through the Agent bridge."""

from dataclasses import asdict, dataclass
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

ModelSafetyAction = Literal[
    "disable_paid_research", "disable_candidate_generation", "disable_training",
    "disable_fast_action", "disable_new_admission", "disable_gray", "disable_all_agents",
    "external_emergency_stop",
]


@dataclass(frozen=True)
class OperatorPolicyEnvelope:
    revision: int
    allowed_symbols: tuple[str, ...]
    max_total_exposure_bps: int
    max_symbol_exposure_bps: int
    max_order_bps: int
    expires_at_ms: int


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


def model_safety_control(
    action: ModelSafetyAction,
    target_id: str,
    policy_envelope_revision: int,
) -> AgentEvent:
    if not target_id or policy_envelope_revision < 0:
        raise ValueError("model safety control identity is required")
    return AgentEvent(
        event_type="model.safety.control",
        payload={
            "contract_version": 2,
            "action": action,
            "target_id": target_id,
            "policy_envelope_revision": policy_envelope_revision,
            "routine_approval": False,
        },
        priority=EventPriority.CRITICAL,
    )


def model_policy_envelope_control(envelope: OperatorPolicyEnvelope) -> AgentEvent:
    if (
        envelope.revision < 0
        or not 1 <= len(envelope.allowed_symbols) <= 5
        or not 0 < envelope.max_total_exposure_bps <= 200
        or not 0 < envelope.max_symbol_exposure_bps <= 50
        or not 0 < envelope.max_order_bps <= 25
        or envelope.expires_at_ms <= 0
    ):
        raise ValueError("invalid operator policy envelope")
    return AgentEvent(
        event_type="model.policy_envelope.configure",
        payload={"contract_version": 2, **asdict(envelope), "routine_approval": False},
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
