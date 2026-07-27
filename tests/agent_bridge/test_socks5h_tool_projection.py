from pathlib import Path

from vnpy.agent_bridge.engine import AgentBridgeEngine
from vnpy.agent_bridge.events import (
    Socks5hToolEvent,
    compute_live_validation_payload_digest,
)
from vnpy.agent_console.evaluation import Socks5hToolViewState


def event(event_id: str = "event-1", revision: int = 1) -> dict:
    payload = {
        "state": "completed",
        "certainty": "certain",
        "freshness": "fresh",
        "evidence_refs": ["blake3:" + "1" * 64],
        "permitted_next_actions": ["inspect"],
    }
    return {
        "contract_version": 1,
        "entity_type": "socks5h_tool_event",
        "event_id": event_id,
        "event_type": "socks5h_tool.result",
        "mission_id": "mission-1",
        "task_id": "task-1",
        "invocation_id": "invocation-1",
        "producer_id": "agentd-1",
        "revision": revision,
        "payload": payload,
        "payload_digest": compute_live_validation_payload_digest(payload),
    }


def test_socks5h_events_are_durable_exact_once_and_isolated_from_live_validation(tmp_path: Path) -> None:
    bridge = AgentBridgeEngine(tmp_path)
    before = bridge.live_validation_snapshot()
    first = event()
    ack = bridge.apply_socks5h_tool_event(first, received_at_ms=2)
    assert ack.status == "applied"
    assert ack.provider_calls == 0
    assert ack.tikhub_route_mutations == 0
    assert bridge.live_validation_snapshot() == before
    bridge.close()

    restarted = AgentBridgeEngine(tmp_path)
    assert len(restarted.socks5h_tool_snapshot()["events"]) == 1
    replayed = restarted.apply_socks5h_tool_event(first, received_at_ms=3)
    assert replayed == ack
    leased = restarted.next_socks5h_tool_ack()
    assert leased == ack
    restarted.confirm_socks5h_tool_ack(leased.event_id, leased.payload_digest)
    restarted.close()

    confirmed = AgentBridgeEngine(tmp_path)
    assert confirmed.next_socks5h_tool_ack() is None
    assert confirmed.live_validation_snapshot() == before
    confirmed.close()


def test_console_is_read_only_and_exposes_separate_tool_views() -> None:
    decoded = Socks5hToolEvent.decode(event())
    state = Socks5hToolViewState().apply(decoded)
    output = state.console_payload()
    assert output["views"]["result"]["state"] == "completed"
    assert output["authority"] == "research_only"
    assert output["invocation_controls"] is False
    assert output["provider_calls"] == 0
    assert output["tikhub_route_mutations"] == 0
