from vnpy.agent_bridge import AgentEvent
from vnpy.agent_console import AgentConsoleEngine


def test_paid_research_projection_exposes_budget_provenance_and_live_path_denials() -> None:
    console = AgentConsoleEngine()
    event = AgentEvent(
        "tikhub.isolation",
        {
            "revision": 1,
            "mission_id": "mission-master-research-1",
            "mission_state": "partial",
            "master_decision_digest": "blake3:" + "1" * 64,
            "purpose": "candidate_hypothesis_research",
            "scope": "600519.SH product demand evidence",
            "provider_budget_remaining": "4.97",
            "provider_cost_certainty": "uncertain",
            "provenance_ref": "blake3:" + "2" * 64,
            "live_observer_allowed": False,
            "live_model_allowed": False,
            "automatic_wakeup_allowed": False,
            "error_code": "UNCERTAIN_OUTCOME",
        },
        correlation_id="correlation-paid-isolation",
    )
    state = console.apply(event)
    isolation = state.tikhub["isolation"]
    assert isolation["provider_budget_remaining"] == "4.97"
    assert isolation["provider_cost_certainty"] == "uncertain"
    assert isolation["provenance_ref"].startswith("blake3:")
    assert isolation["live_observer_allowed"] is False
    assert isolation["live_model_allowed"] is False
    assert isolation["automatic_wakeup_allowed"] is False
    assert state.observer_gate == {}
    assert state.wakeups == {}


def test_paid_result_cannot_claim_a_live_path_or_automatic_wakeup() -> None:
    console = AgentConsoleEngine()
    invalid = AgentEvent(
        "tikhub.isolation",
        {
            "revision": 1,
            "mission_id": "mission-master-research-1",
            "mission_state": "complete",
            "master_decision_digest": "blake3:" + "1" * 64,
            "purpose": "research",
            "scope": "600519.SH",
            "provider_budget_remaining": "4.97",
            "provider_cost_certainty": "known",
            "provenance_ref": "blake3:" + "2" * 64,
            "live_observer_allowed": True,
            "live_model_allowed": False,
            "automatic_wakeup_allowed": False,
            "error_code": None,
        },
        correlation_id="correlation-paid-isolation",
    )
    state = console.apply(invalid)
    assert state.tikhub["isolation"] == {}
    assert state.tikhub["last_error"] == "invalid TikHub isolation"
