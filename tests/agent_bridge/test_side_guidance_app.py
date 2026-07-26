from __future__ import annotations

import os
from typing import Any

from PySide6 import QtWidgets

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import MainWindow
from vnpy.agent_console.app import AgentConsoleApp
from vnpy.agent_console.ui import AgentConsoleWidget, SideMasterGuidanceWidget


class Provider:
    def build_request(self, action: str, **values: Any) -> dict[str, Any]:
        return {"action": action, **values}


class Runner:
    def submit(self, action: str, request: dict[str, Any], callback: Any) -> None:
        callback({"status": "ok", "authoritative_revision": 1, "result": {"action": action}})


def test_real_mainwindow_agent_console_keeps_event_engine_responsive(
    qtbot: Any,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    previous_cwd = os.getcwd()
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(MainWindow, "init_ui", lambda self: None)
    event_engine = EventEngine(interval=0.01)
    main_engine = MainEngine(event_engine)
    main_engine.add_app(AgentConsoleApp)
    window = MainWindow(main_engine, event_engine)
    try:
        window.open_widget(AgentConsoleWidget, AgentConsoleApp.app_name)
        widget = window.widgets[AgentConsoleApp.app_name]
        assert isinstance(widget, AgentConsoleWidget)
        assert isinstance(widget.findChild(QtWidgets.QTabWidget), QtWidgets.QTabWidget)

        observed: list[str] = []
        event_engine.register("guidance.test", lambda event: observed.append(event.data))
        event_engine.put(Event("guidance.test", "responsive"))
        qtbot.waitUntil(lambda: observed == ["responsive"], timeout=1_000)
    finally:
        for widget in window.widgets.values():
            widget.close()
        window.widgets.clear()
        window.deleteLater()
        main_engine.close()
        os.chdir(previous_cwd)


def test_restart_restores_session_health_and_keeps_revoked_draft_isolated(qtbot: Any) -> None:
    response = {
        "status": "ok",
        "authoritative_revision": 7,
        "result": {
            "sessions": [{"session_id": "side-1", "state": "active"}],
            "session": {
                "session_id": "side-1",
                "state": "active",
                "published": False,
                "draft_revision": 3,
                "transcript": [
                    {"speaker": "operator", "content": "draft only research constraint"}
                ],
            },
            "auth": {
                "auth_session_id": "os-session-1",
                "state": "revoked",
                "verification_epoch": 4,
                "revocation_revision": 2,
            },
            "recovery": {
                "run_id": "recovery-1",
                "state": "actionably_blocked",
                "checkpoint_age_ms": 1_000,
                "state_visible_elapsed_ms": 2_000,
                "resume_elapsed_ms": 20_000,
            },
            "retention": [
                {
                    "retention_id": "retention-1",
                    "state": "retaining",
                    "delete_after_ms": 999_999,
                    "basis": "mission_and_derived_strategy_termination_plus_10y",
                }
            ],
            "resources": {
                "ceiling": {"input_tokens": 100},
                "allocated": {"input_tokens": 70},
                "protected": {"input_tokens": 20},
                "reserved": {"input_tokens": 5},
                "consumed": {"input_tokens": 40},
                "remaining": {"input_tokens": 10, "output_tokens": 2},
                "burn_rate": {"input_tokens": 4},
                "projected_usage": {"input_tokens": 95},
                "forecast_horizon_ms": 60_000,
            },
            "starvation": [
                {
                    "finding_id": "starvation-1",
                    "subject_agent_id": "audit",
                    "blocking": True,
                }
            ],
            "acknowledgement": {
                "ack_id": "ack-1",
                "disposition": "blocked",
                "acknowledged_at_ms": 1_200,
                "policy_denial_codes": ["AUTH_REVOKED"],
            },
            "boundary": {
                "boundary_id": "boundary-1",
                "checked_at_ms": 1_300,
                "durable": True,
            },
        },
    }

    def restored_widget() -> SideMasterGuidanceWidget:
        widget = SideMasterGuidanceWidget(
            request_provider=Provider(),
            runner=Runner(),  # type: ignore[arg-type]
        )
        qtbot.addWidget(widget)
        widget.show()
        widget.mission_edit.setText("mission-1")
        widget.session_edit.setText("side-1")
        widget._handle("inspect", response)
        return widget

    first = restored_widget()
    assert first.auth_label.text() == "OS session: revoked (epoch 4, revocation 2)"
    assert "actionably_blocked" in first.recovery_label.text()
    assert "retaining" in first.retention_details.toPlainText()
    assert "protected" in first.resource_details.toPlainText()
    assert first.starvation_label.text() == "Required Agent starvation: 1"
    assert "boundary-1" in first.timeline.toPlainText()
    assert first.timeline.toPlainText().count("semantic_acknowledgement") == 1
    assert "draft only research constraint" in first.transcript.toPlainText()
    assert "draft only research constraint" not in first.preview.toPlainText()
    assert first.unsent_label.isVisible()
    assert not first.turn_button.isEnabled()
    assert not first.prepare_button.isEnabled()
    assert not first.confirm_button.isEnabled()

    restarted = restored_widget()
    restarted._handle("inspect", response)
    assert restarted.timeline.toPlainText().count("semantic_acknowledgement") == 1
    assert restarted.timeline.toPlainText().count("safe_boundary") == 1
    assert restarted.transcript.toPlainText().count("draft only research constraint") == 1
