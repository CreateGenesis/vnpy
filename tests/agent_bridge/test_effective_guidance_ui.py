from __future__ import annotations

from typing import Any

from vnpy.agent_console.guidance import GuidanceViewState, seal_projection_pages
from vnpy.agent_console.ui import SideMasterGuidanceWidget


class RequestProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def build_request(self, action: str, **values: Any) -> dict[str, Any]:
        self.requests.append((action, values))
        return {"action": action, **values}


class Runner:
    def submit(self, action: str, request: dict[str, Any], callback: Any) -> None:
        if action == "effective":
            result = {
                "action": action,
                "history": {
                    "mission_id": "mission-1",
                    "active_notification_ids": ["notification-2"],
                    "history_digest": "blake3:" + "f" * 64,
                    "revisions": [
                        {
                            "revision": 1,
                            "trigger_notification_id": "notification-1",
                            "ack_id": "ack-1",
                            "deferred_member_ids": [],
                        },
                        {
                            "revision": 2,
                            "trigger_notification_id": "notification-2",
                            "ack_id": "ack-2",
                            "deferred_member_ids": [],
                        },
                    ],
                },
            }
            callback({"status": "ok", "authoritative_revision": 2, "result": result})
            return
        if action == "reconcile":
            result = {
                "action": action,
                "decision": {"action": "clarify", "source_snapshot_id": "snapshot-1"},
                "conflict": {
                    "kind": "material",
                    "requires_clarification": True,
                    "preserve_existing": True,
                    "preserve_incoming": True,
                },
                "notification_history": None,
            }
            callback({"status": "ok", "authoritative_revision": 2, "result": result})
            return
        if action == "cancel":
            result = {
                "action": action,
                "cancellation": {
                    "notification_id": "notification-3",
                    "state": "cancelled",
                    "state_revision": 2,
                },
            }
            callback({"status": "cancelled", "authoritative_revision": 2, "result": result})
            return
        callback({"status": "ok", "authoritative_revision": 1, "result": {"action": action}})


def test_effective_history_conflict_and_cancel_are_operable(qtbot: Any) -> None:
    provider = RequestProvider()
    widget = SideMasterGuidanceWidget(
        request_provider=provider,
        runner=Runner(),  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-1")
    widget._session_open = True
    widget._set_session_actions(True)

    widget.inspect_effective_history()
    assert widget.effective_history.topLevelItemCount() == 2
    assert widget.effective_history.topLevelItem(1).text(1) == "active"

    widget.relation_mode.setCurrentText("conflict")
    widget.reconcile_guidance()
    assert "requires_clarification" in widget.preview.toPlainText()
    action, values = provider.requests[-1]
    assert action == "reconcile"
    assert values["payload"]["material_conflict"] is True

    widget._latest_notification = {
        "notification_id": "notification-3",
        "notification_digest": "blake3:" + "3" * 64,
        "state": "pending",
        "state_revision": 1,
    }
    widget.cancel_pending_guidance()
    action, values = provider.requests[-1]
    assert action == "cancel"
    assert values["payload"]["expected_state_revision"] == 1
    assert widget.state_table.item(3, 1).text() == "cancelled"


def test_projection_orders_effective_members_and_classifies_cancellation() -> None:
    def item(identity: str, revision: int, position: int) -> dict[str, Any]:
        return {
            "entity_type": "effective_member",
            "entity_id": identity,
            "entity_revision": revision,
            "state": "active",
            "source_digest": "blake3:" + str(position + 1) * 64,
            "display": {"position": position},
            "exact_content": None,
            "permitted_actions": ["inspect"],
        }

    notification = {
        "entity_type": "notification",
        "entity_id": "notification-1",
        "entity_revision": 1,
        "state": "pending",
        "source_digest": "blake3:" + "a" * 64,
        "display": {"expires_at_ms": 5_000},
        "exact_content": None,
        "permitted_actions": ["inspect", "cancel"],
    }
    page = {
        "entity_type": "guidance_projection_page",
        "contract_version": 1,
        "projection_id": "guidance-projection:mission-1",
        "mission_id": "mission-1",
        "projection_revision": 1,
        "source_revision": 1,
        "freshness": "fresh",
        "certainty": "known",
        "items": [item("notification-2", 2, 1), item("notification-1", 2, 0), notification],
        "cursor": None,
        "next_cursor": None,
        "generated_at_ms": 1_000,
        "projection_digest": "",
    }
    state = GuidanceViewState()
    state.apply(seal_projection_pages([page])[0])
    assert [value["entity_id"] for value in state.effective_documents] == [
        "notification-1",
        "notification-2",
    ]
    assert state.cancellation_eligibility("notification-1", now_ms=4_000) == "eligible"
    assert state.cancellation_eligibility("notification-1", now_ms=5_000) == "expired"
