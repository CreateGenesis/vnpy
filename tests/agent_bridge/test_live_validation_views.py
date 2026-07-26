from time import time_ns

import pytest

from vnpy.agent_bridge.events import (
    LIVE_VALIDATION_EVENT_TYPES,
    compute_live_validation_payload_digest,
)
from vnpy.agent_console.evaluation import LiveValidationViewState
from vnpy.agent_console.models import (
    LiveAuditView,
    LiveBudgetView,
    LiveCallView,
    LiveCampaignView,
    LiveFinalView,
    LiveScorecardView,
    LiveTikHubProvenanceView,
)
from vnpy.agent_console.widget import AgentConsoleWidget


DIGEST = "blake3:" + "1" * 64


def event(event_type: str, event_id: str, item: dict, *, error: str | None = None) -> dict:
    payload = {
        "page_kind": LIVE_VALIDATION_EVENT_TYPES[event_type],
        "page_index": 0,
        "page_size": 1,
        "next_cursor": None,
        "items": [item],
        "certainty": "uncertain" if error else "certain",
        "freshness": "fresh",
        "error_code": error,
        "evidence_refs": ["sha256:" + "2" * 64],
        "permitted_next_actions": ["inspect"],
    }
    return {
        "contract_version": 1,
        "entity_type": "live_validation_event",
        "event_id": event_id,
        "event_type": event_type,
        "campaign_id": "campaign-1",
        "candidate_digest": DIGEST,
        "correlation_id": "correlation-1",
        "producer_id": "agentd-1",
        "producer_epoch": 1,
        "revision": 1,
        "event_time_ms": time_ns() // 1_000_000,
        "payload": payload,
        "previous_payload_digest": None,
        "payload_digest": compute_live_validation_payload_digest(payload),
    }


def test_all_authoritative_views_are_typed_redacted_and_inspectable() -> None:
    fixtures = {
        "live_validation.campaign": {"campaign_id": "campaign-1", "state": "running"},
        "live_validation.route": {"role": "master", "route_digest": DIGEST},
        "live_validation.case": {"case_id": "case-1", "state": "passed"},
        "live_validation.call": {"call_id": "call-1", "state": "completed", "evidence_kind": "live"},
        "live_validation.budget": {"remaining_tokens": 40000, "low_watermark_state": "normal"},
        "live_validation.tikhub_provenance": {"platform": "bilibili", "item_count": 1, "evidence_kind": "live"},
        "live_validation.scorecard": {"score_producer": "harness", "score": 92, "qualified": True},
        "live_validation.audit": {"reviewer_identity": "audit-1", "decision": "approve", "safety_veto": False},
        "live_validation.failure": {"failure_code": "PROVIDER_RATE_LIMIT", "disposition": "known_failed"},
        "live_validation.improvement": {"candidate_digest": DIGEST, "attempt": 1},
        "live_validation.final": {"qualification_source": "harness", "status": "qualified", "audit_quorum": "passed"},
    }
    state = LiveValidationViewState()
    for index, (event_type, item) in enumerate(fixtures.items(), 1):
        state = state.apply(event(event_type, f"event-{index}", item))

    assert isinstance(state.pages["campaign:0"].items[0], LiveCampaignView)
    assert isinstance(state.pages["call:0"].items[0], LiveCallView)
    assert isinstance(state.pages["budget:0"].items[0], LiveBudgetView)
    assert isinstance(
        state.pages["tikhub_provenance:0"].items[0], LiveTikHubProvenanceView
    )
    assert isinstance(state.pages["scorecard:0"].items[0], LiveScorecardView)
    assert isinstance(state.pages["audit:0"].items[0], LiveAuditView)
    assert isinstance(state.pages["final:0"].items[0], LiveFinalView)
    assert state.page("call", query="live")[0]["call_id"] == "call-1"
    assert state.projection_latency_ms < 10_000
    assert "authorization" not in str(state.console_payload()).lower()

    panel = AgentConsoleWidget(live_validation_state=state).panels().live_validation
    assert panel["views"]["final"][0]["status"] == "qualified"
    assert panel["views"]["scorecard"][0]["score_producer"] == "harness"
    assert panel["views"]["audit"][0]["decision"] == "approve"
    assert panel["authority"] == "research_only"
    assert panel["provider_calls"] == 0


def test_only_harness_scores_and_independent_audit_findings_can_qualify() -> None:
    state = LiveValidationViewState()
    with pytest.raises(ValueError, match="NON_HARNESS_SCORE"):
        state.apply(
            event(
                "live_validation.scorecard",
                "model-score",
                {"score_producer": "model", "score": 100, "qualified": True},
            )
        )
    with pytest.raises(ValueError, match="AUDIT_SCORE_FORBIDDEN"):
        state.apply(
            event(
                "live_validation.audit",
                "audit-score",
                {"reviewer_identity": "audit-1", "decision": "approve", "score": 100},
            )
        )
    with pytest.raises(ValueError, match="NON_HARNESS_QUALIFICATION"):
        state.apply(
            event(
                "live_validation.final",
                "model-final",
                {"qualification_source": "master", "status": "qualified"},
            )
        )
