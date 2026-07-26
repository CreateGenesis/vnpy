from __future__ import annotations

from typing import Any

from vnpy.agent_console.ui import SideMasterGuidanceWidget


class Provider:
    def build_request(self, action: str, **values: Any) -> dict[str, Any]:
        return {"action": action, **values}


class Runner:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def submit(self, action: str, request: dict[str, Any], callback: Any) -> None:
        self.requests.append((action, request))
        callback({"status": "ok", "authoritative_revision": 1, "result": {"action": action}})


def test_running_workspace_exposes_sessions_pages_unsent_and_composer(qtbot: Any) -> None:
    runner = Runner()
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=runner)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.show()
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-1")
    widget.open_session()
    widget.turn_editor.setPlainText("focus semiconductor security")
    widget.send_turn()
    assert widget.unsent_label.isVisible()
    assert "focus semiconductor security" in widget.transcript.toPlainText()
    widget.next_transcript_page()
    assert widget.transcript_page_label.text() == "Page 2"
    assert runner.requests[-1][1]["payload"]["page"] == 1


def test_running_projection_renders_resource_forecast_starvation_and_timeline(qtbot: Any) -> None:
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=Runner())  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    vector = {"input_tokens": 100, "output_tokens": 50, "model_calls": 1}
    widget._handle(
        "inspect",
        {
            "status": "ok",
            "authoritative_revision": 3,
            "result": {
                "sessions": [{"session_id": "side-1", "state": "active"}],
                "session": {
                    "transcript": [
                        {"speaker": "operator", "content": "UNSENT"},
                        {"speaker": "side_master", "content": "response"},
                    ],
                    "next_cursor": "cursor-2",
                    "published": False,
                    "effective_guidance_revision": 0,
                },
                "resources": {
                    "ceiling": vector,
                    "allocated": vector,
                    "protected": vector,
                    "reserved": vector,
                    "consumed": vector,
                    "remaining": vector,
                    "burn_rate": vector,
                    "projected_usage": vector,
                    "forecast_horizon_ms": 60000,
                },
                "starvation": [{"subject_agent_id": "audit", "blocking": True}],
                "timeline": [
                    {"state": "transport_delivered", "at_ms": 1000},
                    {"state": "semantically_applied", "at_ms": 1100},
                ],
            },
        },
    )
    assert widget.session_list.count() == 1
    assert "protected" in widget.resource_details.toPlainText()
    assert "projected_usage" in widget.resource_details.toPlainText()
    assert "Required Agent starvation: 1" == widget.starvation_label.text()
    assert "transport_delivered" in widget.timeline.toPlainText()
    assert "semantically_applied" in widget.timeline.toPlainText()


def test_exact_confirmation_remains_digest_bound_and_transport_is_not_semantic(qtbot: Any) -> None:
    runner = Runner()
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=runner)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-1")
    widget.open_session()
    widget._prepared = {
        "media_type": "text/plain; charset=utf-8",
        "body": "focus banks",
        "canonical_body_base64": "Zm9jdXMgYmFua3M=",
        "body_digest": "blake3:" + "a" * 64,
    }
    widget._draft_revision = 1
    widget._confirm_send()
    payload = runner.requests[-1][1]["payload"]
    assert payload["approved_body_digest"] == "blake3:" + "a" * 64
    assert "approved_content" not in payload
    assert "transport" in widget.timeline.toPlainText()
    assert "semantic" not in widget.timeline.toPlainText().lower()


def test_running_boundary_and_master_context_are_read_only_views(qtbot: Any) -> None:
    runner = Runner()
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=runner)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-1")
    widget.open_session()

    widget._handle(
        "running",
        {
            "status": "ok",
            "result": {
                "action": "running",
                "running": {
                    "notification": {"notification_id": "notification-1", "state": "applied"},
                    "boundary": {
                        "boundary_id": "boundary-1",
                        "source": "safe_boundary_receipts",
                        "durable": True,
                    },
                    "interpretation": {"workflow_mutation_permitted": False},
                    "resources": {"remaining": {"input_tokens": 11, "output_tokens": 7}},
                },
            },
        },
    )
    assert widget.state_table.item(3, 1).text() == "applied"
    assert "Running interpretation" in widget.preview.toPlainText()
    assert "boundary-1" in widget.timeline.toPlainText()

    widget._handle(
        "boundary",
        {
            "status": "ok",
            "result": {
                "action": "boundary",
                "boundary": {"boundary_id": "boundary-1", "durable": True},
            },
        },
    )
    assert "boundary-1" in widget.timeline.toPlainText()

    widget._handle(
        "context",
        {
            "status": "ok",
            "result": {
                "action": "context",
                "master_context": {
                    "mission_id": "mission-1",
                    "guidance": {"resources": {"remaining": {"input_tokens": 9}}},
                },
            },
        },
    )
    assert "Master context" in widget.preview.toPlainText()
    assert runner.requests[-1][0] == "open"


def test_timeline_is_complete_ordered_and_deduplicated_across_projection_refreshes(
    qtbot: Any,
) -> None:
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=Runner())  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    page = {
        "entity_type": "guidance_projection_page",
        "contract_version": 1,
        "projection_id": "projection:mission-1",
        "mission_id": "mission-1",
        "projection_revision": 1,
        "source_revision": 1,
        "freshness": "fresh",
        "certainty": "known",
        "items": [
            {
                "entity_type": "notification",
                "entity_id": "notification-1",
                "entity_revision": 2,
                "state": "applied",
                "source_digest": "blake3:" + "a" * 64,
                "display": {"created_at_ms": 100},
                "permitted_actions": ["inspect"],
            },
            {
                "entity_type": "acknowledgement",
                "entity_id": "ack-1",
                "entity_revision": 1,
                "state": "blocked",
                "source_digest": "blake3:" + "b" * 64,
                "display": {
                    "acknowledged_at_ms": 110,
                    "policy_denial_codes": ["FORBIDDEN"],
                },
                "permitted_actions": ["inspect"],
            },
            {
                "entity_type": "recovery_run",
                "entity_id": "recovery-1",
                "entity_revision": 1,
                "state": "completed",
                "source_digest": "blake3:" + "c" * 64,
                "display": {"completed_at_ms": 130},
                "permitted_actions": ["inspect"],
            },
        ],
        "cursor": None,
        "next_cursor": None,
        "generated_at_ms": 200,
        "projection_digest": "",
    }
    from vnpy.agent_console.guidance import seal_projection_pages

    pages = seal_projection_pages([page])
    response = {"status": "ok", "result": {"projection_pages": pages}}
    widget._handle("inspect", response)
    first = widget.timeline.toPlainText()
    widget._handle("inspect", response)
    second = widget.timeline.toPlainText()
    assert first == second
    assert "applied" in first
    assert "semantic_acknowledgement" in first
    assert "policy_denial" in first
    assert "recovered" in first
    assert first.index("applied") < first.index("semantic_acknowledgement") < first.index("recovered")


def test_timeline_surfaces_command_errors_cancellation_expiry_and_close(qtbot: Any) -> None:
    widget = SideMasterGuidanceWidget(request_provider=Provider(), runner=Runner())  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    widget._handle(
        "send",
        {
            "status": "blocked",
            "error": {"code": "FORBIDDEN", "message": "policy denied"},
        },
    )
    widget._handle(
        "inspect",
        {
            "status": "ok",
            "result": {
                "timeline": [
                    {"state": "cancelled", "at_ms": 20, "event_id": "event-cancel"},
                    {"state": "expired", "at_ms": 30, "event_id": "event-expire"},
                    {"state": "closed", "at_ms": 40, "event_id": "event-close"},
                ]
            },
        },
    )
    timeline = widget.timeline.toPlainText()
    assert "policy_denial" in timeline
    assert "cancelled" in timeline
    assert "expired" in timeline
    assert "closed" in timeline


def test_health_recovery_disable_and_server_permitted_actions_are_operable(qtbot: Any) -> None:
    runner = Runner()
    widget = SideMasterGuidanceWidget(
        request_provider=Provider(),
        runner=runner,  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-1")
    widget._session_open = True
    widget._set_session_actions(True)

    vector = {"input_tokens": 100, "output_tokens": 50, "model_calls": 1}
    widget._handle(
        "health",
        {
            "status": "ok",
            "authoritative_revision": 7,
            "result": {
                "recovery": {
                    "state": "actionably_blocked",
                    "checkpoint_age_ms": 8_000,
                    "state_visible_elapsed_ms": 400,
                    "resume_or_block_elapsed_ms": 1_200,
                    "pending_same_id_replays": 2,
                },
                "route": {"state": "ready", "route_digest": "blake3:" + "a" * 64},
                "bridge": {"state": "degraded", "queue_depth": 3},
                "projection": {"mission_count": 1, "maximum_revision": 9},
                "resources": {"remaining": vector, "recovery_blocked_count": 1},
                "retention": {"record_count": 4, "hold_count": 1},
            },
            "health": {"state": "degraded", "source_revision": 7},
            "permitted_next_actions": ["health", "inspect", "recover", "disable", "close"],
        },
    )
    details = widget.health_details.toPlainText()
    assert "actionably_blocked" in details
    assert '"queue_depth": 3' in details
    assert "pending_same_id_replays" in details
    assert widget.recover_button.isEnabled()
    assert widget.disable_button.isEnabled()

    widget.recover_guidance()
    assert runner.requests[-1][0] == "recover"
    assert runner.requests[-1][1]["payload"]["recovery_action"] == "mark_blocked"
    widget.disable_guidance()
    assert runner.requests[-1][0] == "disable"
    assert runner.requests[-1][1]["payload"]["expected_policy_revision"] == 7


def test_revoked_auth_and_rejected_projection_preserve_last_known_valid_state(qtbot: Any) -> None:
    widget = SideMasterGuidanceWidget(
        request_provider=Provider(),
        runner=Runner(),  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget._session_open = True
    widget.turn_editor.setPlainText("unsent local guidance")
    widget._render_auth({"state": "revoked", "verification_epoch": 2, "revocation_revision": 3})
    assert not widget.turn_button.isEnabled()
    assert widget.inspect_button.isEnabled()
    assert widget.turn_editor.toPlainText() == "unsent local guidance"

    from vnpy.agent_console.guidance import seal_projection_pages

    page = {
        "entity_type": "guidance_projection_page",
        "contract_version": 1,
        "projection_id": "projection:mission-1",
        "mission_id": "mission-1",
        "projection_revision": 1,
        "source_revision": 1,
        "freshness": "fresh",
        "certainty": "known",
        "items": [],
        "cursor": None,
        "next_cursor": None,
        "generated_at_ms": 100,
        "projection_digest": "",
    }
    accepted = seal_projection_pages([page])[0]
    widget._apply_projection_pages([accepted])
    assert widget._guidance_state.projection_revision == 1

    rejected = dict(accepted)
    rejected["source_revision"] = 2
    widget._apply_projection_pages([rejected])
    assert widget._guidance_state.projection_revision == 1
    assert "last known valid" in widget.health_details.toPlainText().lower()
