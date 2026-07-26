from time import time_ns

from vnpy.agent_bridge.events import (
    LIVE_VALIDATION_EVENT_TYPES,
    compute_live_validation_payload_digest,
)
from vnpy.agent_console.evaluation import LiveValidationViewState
from vnpy.agent_console.ui import LiveValidationQtWidget
from vnpy.agent_console.widget import AgentConsoleWidget


DIGEST = "blake3:" + "1" * 64


def event(
    event_id: str,
    event_type: str,
    item: dict,
    *,
    index: int = 0,
    certainty: str = "certain",
    freshness: str = "fresh",
    error_code: str | None = None,
    next_cursor: str | None = None,
) -> dict:
    payload = {
        "page_kind": LIVE_VALIDATION_EVENT_TYPES[event_type],
        "page_index": index,
        "page_size": 1,
        "next_cursor": next_cursor,
        "items": [item],
        "certainty": certainty,
        "freshness": freshness,
        "error_code": error_code,
        "evidence_refs": ["sha256:" + "2" * 64],
        "permitted_next_actions": ["inspect", "evidence"],
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


def journey_state() -> LiveValidationViewState:
    state = LiveValidationViewState()
    for index, evidence_kind in enumerate(("live", "fixture", "cached", "dry_run", "uncertain")):
        state = state.apply(
            event(
                f"call-{index}",
                "live_validation.call",
                {"call_id": f"call-{index}", "evidence_kind": evidence_kind, "state": evidence_kind},
                index=index,
                certainty="uncertain" if evidence_kind == "uncertain" else "certain",
                freshness="stale" if evidence_kind == "cached" else "fresh",
                next_cursor="next" if index < 4 else None,
            )
        )
    state = state.apply(
        event(
            "budget-low",
            "live_validation.budget",
            {"remaining_tokens": 800, "low_watermark_state": "conserve"},
            error_code="BUDGET_LOW",
        )
    )
    state = state.apply(
        event(
            "final",
            "live_validation.final",
            {"qualification_source": "harness", "status": "blocked", "audit_quorum": "pending"},
        )
    )
    return state


def test_programmatic_console_journey_covers_pages_filters_labels_budget_and_final_state() -> None:
    state = journey_state()

    widget = AgentConsoleWidget(live_validation_state=state)
    panel = widget.panels().live_validation
    assert {item["evidence_kind"] for item in panel["views"]["call"]} == {
        "live",
        "fixture",
        "cached",
        "dry_run",
        "uncertain",
    }
    assert state.page("call", 3, query="dry_run")[0]["call_id"] == "call-3"
    assert panel["pages"]["call"][0]["next_cursor"] == "next"
    assert panel["pages"]["call"][2]["freshness"] == "stale"
    assert panel["pages"]["call"][4]["certainty"] == "uncertain"
    assert panel["budget_low_watermarks"][0]["low_watermark_state"] == "conserve"
    assert panel["errors"] == ["BUDGET_LOW"]
    assert panel["permitted_next_actions"] == ["evidence", "inspect"]
    assert panel["views"]["final"][0]["status"] == "blocked"
    assert not hasattr(widget, "submit_order")
    assert not hasattr(widget, "approve_release")


def test_offscreen_qt_journey_drives_real_filters_pages_budget_and_final(qtbot) -> None:
    widget = LiveValidationQtWidget()
    qtbot.addWidget(widget)
    widget.show()

    with qtbot.waitSignal(widget.state_received, timeout=1_000):
        widget.update_live_validation(journey_state())

    widget.kind_combo.setCurrentText("call")
    widget.page_spin.setValue(3)
    qtbot.keyClicks(widget.query_edit, "dry_run")
    assert widget.table.rowCount() == 1
    assert "certain" in widget.status_label.text()

    widget.query_edit.clear()
    widget.kind_combo.setCurrentText("budget")
    assert widget.table.rowCount() == 1
    assert "BUDGET_LOW" in widget.status_label.text()

    widget.kind_combo.setCurrentText("final")
    assert widget.table.rowCount() == 1
    headers = [
        widget.table.horizontalHeaderItem(index).text()
        for index in range(widget.table.columnCount())
    ]
    status_column = headers.index("status")
    assert widget.table.item(0, status_column).text() == "blocked"
    assert not hasattr(widget, "submit_order")
    assert not hasattr(widget, "approve_release")
