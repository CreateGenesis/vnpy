from time import time_ns

from vnpy.agent_bridge import AgentEvent
from vnpy.agent_console import AgentConsoleEngine, AgentConsoleWidget
from vnpy.agent_console.controls import research_control


def test_console_projects_correlated_revisioned_gate_and_coverage_state() -> None:
    console = AgentConsoleEngine()
    gate = AgentEvent(
        "observer.gate",
        {"status": "completed", "registry_digest": "a" * 64, "revision": 3},
    )
    state = console.apply(gate)
    assert state.correlation_id == gate.correlation_id
    assert state.observer_gate["registry_digest"] == "a" * 64
    assert state.source_revisions["observer_gate"] == 3
    assert state.revision == 1

    coverage = AgentEvent(
        "coverage.state",
        {"ratio": 1.0, "positive": 12, "negative": 12, "revision": 2},
    )
    state = console.apply(coverage)
    assert state.coverage["ratio"] == 1.0
    assert state.correlation_id == coverage.correlation_id


def test_stale_projection_cannot_overwrite_last_known_degraded_state() -> None:
    console = AgentConsoleEngine()
    failed = AgentEvent("bridge.health", {"state": "failed", "revision": 5})
    stale = AgentEvent("bridge.health", {"state": "healthy", "revision": 4})
    assert console.apply(failed).bridge_health == "failed"
    state = console.apply(stale)
    assert state.bridge_health == "failed"
    assert state.last_error == "stale research event: bridge.health"
    assert state.source_revisions["bridge_health"] == 5


def test_console_projects_lanes_latency_recovery_missions_artifacts_and_audits() -> None:
    console = AgentConsoleEngine()
    fixtures = {
        "bridge.lanes": ("lanes", {"critical_depth": 1, "routine_depth": 2}),
        "bridge.latency": ("latency", {"p99_ms": 12.0}),
        "recovery.state": ("recovery", {"state": "complete", "ack_watermark": 9}),
        "mission.state": ("missions", {"mission-1": "completed"}),
        "artifact.state": ("artifacts", {"digest": "b" * 64}),
        "audit.state": ("audits", {"quorum": "approved", "reviewers": 3}),
    }
    for revision, (event_type, (field_name, payload)) in enumerate(fixtures.items(), start=1):
        event = AgentEvent(event_type, {**payload, "revision": revision})
        state = console.apply(event)
        assert getattr(state, field_name)


def test_projection_is_applied_within_five_seconds() -> None:
    console = AgentConsoleEngine()
    event_time_ms = time_ns() // 1_000_000
    event = AgentEvent(
        "budget.ledger",
        {"remaining": {"tokens": 90_000}, "revision": 1},
        event_time_ms=event_time_ms,
    )
    state = console.apply(event)
    assert state.budgets["remaining"]["tokens"] == 90_000
    assert state.projection_latency_ms <= 5_000


def test_research_controls_remain_research_only() -> None:
    for action in (
        "create",
        "pause",
        "resume",
        "retry",
        "cancel",
        "revoke_qualification",
        "disable_capability",
        "stop_evaluation",
    ):
        event = research_control(action, "target-1")
        assert event.event_type == "research.control"
        assert event.payload == {
            "action": action,
            "target_id": "target-1",
            "contract_version": 1,
        }
        assert "order" not in event.payload


def test_widget_exposes_operational_panels_without_approval_controls() -> None:
    console = AgentConsoleEngine()
    state = console.apply(
        AgentEvent("workflow.state", {"revision": 1, "dag": ["observe", "plan"]})
    )
    state = console.apply(
        AgentEvent("route.state", {"revision": 1, "master": "gpt-5.6-sol"})
    )
    state = console.apply(
        AgentEvent("lifecycle.result", {"revision": 1, "status": "rejected"})
    )
    panels = AgentConsoleWidget(state).panels()
    assert panels.workflow["dag"] == ["observe", "plan"]
    assert panels.routes["master"] == "gpt-5.6-sol"
    assert panels.lifecycle["results"]["status"] == "rejected"
    assert not hasattr(panels, "approve")


def test_console_routes_mcp_health_through_the_redacted_projection() -> None:
    console = AgentConsoleEngine()
    state = console.apply(
        AgentEvent(
            "mcp.health",
            {
                "revision": 1,
                "status": "blocked",
                "implicit_context": False,
                "error": {
                    "code": "UPSTREAM_UNAVAILABLE",
                    "message": "Authorization: Bearer forbidden",
                },
            },
        )
    )
    assert state.mcp["calls"]["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert "Authorization" not in str(state.mcp)
    panels = AgentConsoleWidget(state).panels()
    assert panels.mcp["calls"]["status"] == "blocked"


def test_tikhub_and_generic_mcp_have_independent_revisions_errors_and_controls() -> None:
    console = AgentConsoleEngine()
    console.apply(AgentEvent("mcp.health", {"revision": 7, "status": "blocked", "implicit_context": False, "error": {"code": "MCP_UPSTREAM", "message": "blocked"}}))
    state = console.apply(AgentEvent("tikhub.health", {
        "revision": 3, "state": "degraded", "route_mode": "required_socks5h",
        "checked_at_ms": 1, "latency_ms": 5, "provider_request_id": None,
        "error_code": "UPSTREAM_UNAVAILABLE",
    }))
    panels = AgentConsoleWidget(state).panels()
    assert panels.mcp["revision"] == 1
    assert panels.tikhub["source_revisions"]["health"] == 3
    assert panels.mcp["calls"]["error_code"] == "MCP_UPSTREAM"
    assert panels.tikhub["health"]["error_code"] == "UPSTREAM_UNAVAILABLE"
    assert panels.tikhub is not panels.mcp
