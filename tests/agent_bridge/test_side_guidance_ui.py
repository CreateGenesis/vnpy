from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from vnpy.agent_console.app import AgentConsoleApp
from vnpy.agent_console.guidance import seal_projection_pages
from vnpy.agent_console.ui import AgentConsoleWidget, SideMasterGuidanceWidget


class FakeRequestProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def build_request(self, action: str, **values: Any) -> dict[str, Any]:
        self.requests.append((action, values))
        return {"action": action, **values}


class FakeRunner:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def submit(self, action: str, request: dict[str, Any], callback: Any) -> None:
        self.requests.append((action, request))
        revision = {"open": 0, "turn": 1, "prepare": 1, "send": 2, "inspect": 2}.get(
            action, 3
        )
        result: dict[str, Any] = {"action": action}
        if action == "prepare":
            approved = request["payload"]["approved_content"]
            result.update(
                {
                    "draft_id": "draft:side-test",
                    "draft_revision": 1,
                    "preview": {
                        "approved_content": approved,
                        "interpretation": _content("Interpret this as a research focus"),
                        "source_mission_revision": 1,
                        "current_mission_revision": 2,
                        "current_effective_revision": 0,
                        "stale_snapshot_warning": True,
                    },
                }
            )
        if action == "send":
            result.update({"delivery_state": "pending"})
        if action == "inspect":
            result.update(
                {
                    "session": {
                        "draft_revision": 1,
                        "snapshot_digest": "blake3:" + "1" * 64,
                        "effective_guidance_revision": 0,
                        "initial_guidance_gate": {
                            "transitions": [{"state": "awaiting_initial_ack"}]
                        },
                    }
                }
            )
        callback(
            {
                "status": "pending" if action in {"turn", "send"} else "ok",
                "authoritative_revision": revision,
                "result": result,
                "usage": {
                    "remaining": {"input_tokens": 9000, "output_tokens": 3000}
                },
            }
        )


def test_real_vnpy_widget_opens_dynamic_draft_and_confirms_exact_digest(qtbot: Any) -> None:
    provider = FakeRequestProvider()
    runner = FakeRunner()
    widget = SideMasterGuidanceWidget(
        request_provider=provider,
        runner=runner,  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-test")
    widget.open_session()
    assert widget.turn_button.isEnabled()
    assert widget._revision == 0

    widget.content_mode.setCurrentText("JSON")
    widget.turn_editor.setPlainText('{"focus":["600000.SH"],"avoid":{"sector":"bank"}}')
    widget.prepare_draft()
    assert widget._draft_revision == 1
    assert widget._prepared is not None
    prepared_digest = widget._prepared["body_digest"]
    widget._confirm_send()

    send_action, send_values = provider.requests[-1]
    assert send_action == "send"
    assert send_values["payload"]["approved_body_digest"] == prepared_digest
    assert send_values["payload"]["expected_draft_revision"] == 1
    assert "approved_content" not in send_values["payload"]
    assert widget.state_table.item(3, 1).text() == "pending"
    assert widget.state_table.item(4, 1).text() == "in 9000 / out 3000"


def test_agent_console_mainwindow_surface_is_a_real_qwidget(
    qtbot: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
    widget = AgentConsoleWidget(object(), object())
    qtbot.addWidget(widget)
    assert isinstance(widget, QtWidgets.QWidget)
    tabs = widget.findChild(QtWidgets.QTabWidget)
    assert tabs is not None
    assert isinstance(tabs.widget(0), SideMasterGuidanceWidget)
    assert AgentConsoleApp.widget_name == "AgentConsoleWidget"
    assert AgentConsoleApp.engine_class.__name__ == "AgentConsoleEngine"


def test_arbitrary_json_scalar_round_trips_as_dynamic_content(qtbot: Any) -> None:
    widget = SideMasterGuidanceWidget(
        request_provider=FakeRequestProvider(),
        runner=FakeRunner(),  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget.content_mode.setCurrentText("JSON")
    widget.turn_editor.setPlainText("42")
    content = widget._editor_content()
    assert content is not None
    assert content["body"] == 42
    assert content["media_type"] == "application/json"


def test_template_is_only_an_editable_seed_and_preview_separates_interpretation(
    qtbot: Any, tmp_path: Any
) -> None:
    template_root = tmp_path / "auto-tride-agent" / "guidance-templates"
    template_root.mkdir(parents=True)
    (template_root / "initial.json").write_text(
        '{"enabled":true,"template_id":"initial","label":"A-share",'
        '"body":{"focus":["liquidity"]}}',
        encoding="utf-8",
    )
    widget = SideMasterGuidanceWidget(
        request_provider=FakeRequestProvider(),
        runner=FakeRunner(),  # type: ignore[arg-type]
        workspace_root=tmp_path,
    )
    qtbot.addWidget(widget)
    widget.template_combo.setCurrentIndex(1)
    assert "liquidity" in widget.turn_editor.toPlainText()
    widget.turn_editor.setPlainText("42")
    widget.content_mode.setCurrentText("JSON")
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-test")
    widget.open_session()
    widget.prepare_draft()
    assert widget._prepared is not None
    assert widget._prepared["body"] == 42
    assert "Side Master interpretation" in widget.preview.toPlainText()
    assert "STALE SNAPSHOT" in widget.preview.toPlainText()


def test_inspect_projects_initial_gate_without_opening_it_locally(qtbot: Any) -> None:
    widget = SideMasterGuidanceWidget(
        request_provider=FakeRequestProvider(),
        runner=FakeRunner(),  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    widget.mission_edit.setText("mission-1")
    widget.session_edit.setText("side-test")
    widget.open_session()
    widget.inspect_session()
    gate_rows = [
        row for row in range(widget.state_table.rowCount())
        if widget.state_table.item(row, 0).text() == "Gate"
    ]
    assert widget.state_table.item(gate_rows[0], 1).text() == "awaiting_initial_ack"
    assert widget.confirm_button.isEnabled() is False


def test_inspect_incrementally_renders_notification_ack_effective_gate_and_allocation(
    qtbot: Any,
) -> None:
    widget = SideMasterGuidanceWidget(
        request_provider=FakeRequestProvider(),
        runner=FakeRunner(),  # type: ignore[arg-type]
    )
    qtbot.addWidget(widget)
    approved = _content("Only research liquid A-share equities")
    interpretation = _content("Treat liquidity as a mission research preference")
    acknowledgement = {
        "understood_intent": _content("Research liquid A-share equities"),
        "expected_effect": _content("Prioritize liquid names"),
    }

    def projection_item(
        kind: str,
        identity: str,
        revision: int,
        state: str,
        *,
        display: dict[str, Any] | None = None,
        exact: Any = None,
    ) -> dict[str, Any]:
        return {
            "entity_type": kind,
            "entity_id": identity,
            "entity_revision": revision,
            "state": state,
            "source_digest": "blake3:" + "a" * 64,
            "display": display or {},
            "exact_content": exact,
            "permitted_actions": ["inspect"],
        }

    pages = seal_projection_pages(
        [
            {
                "entity_type": "guidance_projection_page",
                "contract_version": 1,
                "projection_id": "guidance-projection:mission-1",
                "mission_id": "mission-1",
                "projection_revision": 1,
                "source_revision": 1,
                "freshness": "fresh",
                "certainty": "known",
                "items": [
                    projection_item(
                        "notification",
                        "notification-1",
                        2,
                        "applied",
                        exact={
                            "approved_content": approved,
                            "interpretation": interpretation,
                        },
                    ),
                    projection_item(
                        "acknowledgement",
                        "ack-1",
                        1,
                        "applied",
                        exact=acknowledgement,
                    ),
                    projection_item(
                        "health",
                        "effective-guidance:mission-1",
                        1,
                        "active",
                    ),
                    projection_item(
                        "health",
                        "initial-guidance-gate:mission-1",
                        2,
                        "ready_for_autonomy",
                    ),
                    projection_item(
                        "agent_allocation",
                        "allocation:mission-1:side-master:side-test",
                        2,
                        "active",
                        display={
                            "remaining": {"input_tokens": 7000, "output_tokens": 2000}
                        },
                    ),
                ],
                "cursor": None,
                "next_cursor": None,
                "generated_at_ms": 1_000,
                "projection_digest": "",
            }
        ]
    )
    widget._handle(
        "inspect",
        {
            "status": "ok",
            "authoritative_revision": 2,
            "result": {
                "projection_pages": pages,
                "session": {"effective_guidance_revision": 1},
            },
        },
    )
    rows = {
        widget.state_table.item(row, 0).text(): widget.state_table.item(row, 1).text()
        for row in range(widget.state_table.rowCount())
    }
    assert rows["Gate"] == "ready_for_autonomy"
    assert rows["Effective"] == "1"
    assert rows["Resources"] == "in 7000 / out 2000"
    assert "Side Master interpretation" in widget.preview.toPlainText()
    assert "Master acknowledgement" in widget.preview.toPlainText()


def _content(value: str) -> dict[str, Any]:
    import base64
    import blake3

    raw = value.encode("utf-8")
    return {
        "media_type": "text/plain; charset=utf-8",
        "body": value,
        "canonical_body_base64": base64.b64encode(raw).decode("ascii"),
        "body_digest": f"blake3:{blake3.blake3(raw).hexdigest()}",
    }
