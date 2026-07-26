"""Qt workspace for authenticated Side Master guidance."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Callable, Mapping
import json
import os
from pathlib import Path
from time import time_ns
from typing import Any, Protocol
from uuid import uuid4

import blake3
from PySide6 import QtCore, QtGui, QtWidgets

from .evaluation import LiveValidationViewState

from vnpy.agent_bridge.operator_session import OsSessionIdentityProvider
from vnpy.agent_console.guidance import GuidanceViewState


MAX_BODY_BYTES = 32 * 1024


class _RequestProvider(Protocol):
    def build_request(
        self,
        action: str,
        *,
        payload: Mapping[str, Any],
        mission_id: str | None = None,
        session_id: str | None = None,
        expected_revision: int = 0,
        **identities: Any,
    ) -> dict[str, Any]: ...


ResponseCallback = Callable[[dict[str, Any]], None]


class GuidanceCommandRunner(QtCore.QObject):
    """Run one bounded agentctl request without blocking Qt's event loop."""

    def __init__(self, workspace_root: Path, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._root = workspace_root
        self._process: QtCore.QProcess | None = None
        self._callback: ResponseCallback | None = None
        self._stdout = bytearray()
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)

    @property
    def busy(self) -> bool:
        return self._process is not None

    def submit(
        self,
        action: str,
        request: Mapping[str, Any],
        callback: ResponseCallback,
    ) -> None:
        if self.busy:
            callback(_local_error("BACKPRESSURE", "another guidance command is running"))
            return
        process = QtCore.QProcess(self)
        self._process = process
        self._callback = callback
        self._stdout.clear()
        process.setProgram(_agentctl_program(self._root))
        process.setArguments(["guidance", action, "--request", "-"])
        process.setWorkingDirectory(str(self._root))
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        environment.insert("AGENT_WORKSPACE_ROOT", str(self._root))
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        process.started.connect(
            lambda: self._write_request(
                json.dumps(
                    dict(request),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
        )
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        process.start()
        self._timeout.start(5_000)

    def _write_request(self, payload: bytes) -> None:
        if self._process is None:
            return
        self._process.write(payload)
        self._process.closeWriteChannel()

    def _read_stdout(self) -> None:
        if self._process is not None:
            self._stdout.extend(bytes(self._process.readAllStandardOutput()))

    def _finished(self, _exit_code: int, _status: QtCore.QProcess.ExitStatus) -> None:
        self._read_stdout()
        try:
            response = json.loads(self._stdout, object_pairs_hook=_reject_duplicate_keys)
            if not isinstance(response, dict):
                raise ValueError
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            response = _local_error("INTERNAL", "agentctl returned an invalid response")
        self._complete(response)

    def _process_error(self, error: QtCore.QProcess.ProcessError) -> None:
        if error == QtCore.QProcess.ProcessError.Crashed:
            self._complete(_local_error("INTERNAL", "agentctl stopped unexpectedly"))
        elif error == QtCore.QProcess.ProcessError.FailedToStart:
            self._complete(_local_error("UNAVAILABLE", "agentctl is unavailable"))

    def _on_timeout(self) -> None:
        if self._process is not None:
            self._process.kill()
        self._complete(_local_error("UNAVAILABLE", "guidance command timed out"))

    def _complete(self, response: dict[str, Any]) -> None:
        if self._process is None:
            return
        self._timeout.stop()
        process, callback = self._process, self._callback
        self._process = None
        self._callback = None
        process.deleteLater()
        if callback is not None:
            callback(response)


class SideMasterGuidanceWidget(QtWidgets.QWidget):
    """Independent Side Master conversation and exact publication workspace."""

    def __init__(
        self,
        main_engine: Any | None = None,
        event_engine: Any | None = None,
        *,
        request_provider: _RequestProvider | None = None,
        runner: GuidanceCommandRunner | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.main_engine = main_engine
        self.event_engine = event_engine
        self._root = Path(
            workspace_root or os.environ.get("AGENT_WORKSPACE_ROOT", Path.cwd())
        ).resolve()
        self._provider = request_provider or OsSessionIdentityProvider(
            state_dir=self._root / ".agent-state"
        )
        self._runner = runner or GuidanceCommandRunner(self._root, self)
        self._revision = 0
        self._draft_revision = 0
        self._prepared: dict[str, Any] | None = None
        self._prepared_preview: dict[str, Any] | None = None
        self._latest_notification: dict[str, Any] | None = None
        self._session_open = False
        self._templates: list[dict[str, Any] | None] = [None]
        self._guidance_state = GuidanceViewState()
        self._timeline_entries: dict[str, dict[str, Any]] = {}
        self._timeline_serial = 0
        self._transcript_cursor: str | None = None
        self._transcript_page = 0
        self._transcript_next_cursor: str | None = None
        self._auth_writable = True
        self._guidance_enabled = True
        self._policy_revision = 0
        self._permitted_actions: set[str] = set()
        self._health_sections: dict[str, Any] = {}
        self._projection_status = "waiting for authoritative snapshot"
        self._build_ui()
        self._load_templates()
        self._set_session_actions(False)

    def _build_ui(self) -> None:
        self.setWindowTitle("Side Master Guidance")
        self.resize(1180, 760)
        root_layout = QtWidgets.QVBoxLayout(self)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        session_bar = QtWidgets.QHBoxLayout()
        self.mission_edit = QtWidgets.QLineEdit("default")
        self.mission_edit.setPlaceholderText("Mission ID")
        self.session_edit = QtWidgets.QLineEdit(f"side-{uuid4().hex[:10]}")
        self.session_edit.setPlaceholderText("Session ID")
        self.template_combo = QtWidgets.QComboBox()
        self.open_button = self._button(
            "Open",
            QtWidgets.QStyle.StandardPixmap.SP_DialogOpenButton,
            self.open_session,
        )
        self.new_button = self._button(
            "New",
            QtWidgets.QStyle.StandardPixmap.SP_FileIcon,
            self.reset_session,
        )
        session_bar.addWidget(self.mission_edit, 2)
        session_bar.addWidget(self.session_edit, 2)
        session_bar.addWidget(self.template_combo, 2)
        session_bar.addWidget(self.open_button)
        session_bar.addWidget(self.new_button)
        root_layout.addLayout(session_bar)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        conversation = QtWidgets.QWidget()
        conversation_layout = QtWidgets.QVBoxLayout(conversation)
        conversation_layout.setContentsMargins(0, 0, 0, 0)
        self.transcript = QtWidgets.QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Side session transcript")
        self.unsent_label = QtWidgets.QLabel("Unsent side conversation")
        self.unsent_label.setObjectName("unsentGuidanceLabel")
        self.unsent_label.setVisible(False)
        self.session_list = QtWidgets.QListWidget()
        self.session_list.setObjectName("sideSessionList")
        self.session_list.setMaximumHeight(82)
        self.session_list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        page_bar = QtWidgets.QHBoxLayout()
        self.previous_page_button = self._button(
            "Previous page", QtWidgets.QStyle.StandardPixmap.SP_ArrowLeft, self.previous_transcript_page
        )
        self.next_page_button = self._button(
            "Next page", QtWidgets.QStyle.StandardPixmap.SP_ArrowRight, self.next_transcript_page
        )
        self.transcript_page_label = QtWidgets.QLabel("Page 1")
        page_bar.addWidget(self.previous_page_button)
        page_bar.addWidget(self.transcript_page_label)
        page_bar.addWidget(self.next_page_button)
        page_bar.addStretch(1)
        self.turn_editor = QtWidgets.QPlainTextEdit()
        self.turn_editor.setPlaceholderText("Write guidance, a goal, a constraint, or a task")
        self.turn_editor.setMinimumHeight(150)
        turn_bar = QtWidgets.QHBoxLayout()
        self.content_mode = QtWidgets.QComboBox()
        self.content_mode.addItems(["Text", "JSON"])
        self.relation_mode = QtWidgets.QComboBox()
        self.relation_mode.setObjectName("guidanceRelationMode")
        self.relation_mode.addItems(["add", "replace", "withdraw", "supersede", "conflict"])
        self.relation_target = QtWidgets.QLineEdit()
        self.relation_target.setObjectName("guidanceRelationTarget")
        self.relation_target.setPlaceholderText("Target notification ID")
        self.turn_button = self._button(
            "Send",
            QtWidgets.QStyle.StandardPixmap.SP_ArrowForward,
            self.send_turn,
        )
        self.prepare_button = self._button(
            "Prepare",
            QtWidgets.QStyle.StandardPixmap.SP_DialogSaveButton,
            self.prepare_draft,
        )
        turn_bar.addWidget(self.content_mode)
        turn_bar.addWidget(self.relation_mode)
        turn_bar.addWidget(self.relation_target, 2)
        turn_bar.addStretch(1)
        turn_bar.addWidget(self.turn_button)
        turn_bar.addWidget(self.prepare_button)
        conversation_layout.addWidget(self.session_list)
        conversation_layout.addWidget(self.unsent_label)
        conversation_layout.addWidget(self.transcript, 3)
        conversation_layout.addLayout(page_bar)
        conversation_layout.addWidget(self.turn_editor, 2)
        conversation_layout.addLayout(turn_bar)
        splitter.addWidget(conversation)

        status_panel = QtWidgets.QWidget()
        status_layout = QtWidgets.QVBoxLayout(status_panel)
        status_layout.setContentsMargins(8, 0, 0, 0)
        self.state_table = QtWidgets.QTableWidget(8, 2)
        self.state_table.setHorizontalHeaderLabels(["State", "Value"])
        self.state_table.verticalHeader().setVisible(False)
        self.state_table.horizontalHeader().setStretchLastSection(True)
        self.state_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.state_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        for row, name in enumerate(
            ["Session", "Revision", "Draft", "Delivery", "Resources", "Gate", "Effective", "Snapshot"]
        ):
            self.state_table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
            self.state_table.setItem(row, 1, QtWidgets.QTableWidgetItem("-"))
        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Prepared exact content")
        self.resource_details = QtWidgets.QPlainTextEdit()
        self.resource_details.setObjectName("resourceForecastView")
        self.resource_details.setReadOnly(True)
        self.resource_details.setPlaceholderText("Mission resource forecast")
        self.timeline = QtWidgets.QPlainTextEdit()
        self.timeline.setObjectName("guidanceTimeline")
        self.timeline.setReadOnly(True)
        self.timeline.setPlaceholderText("Transport and semantic timeline")
        self.effective_history = QtWidgets.QTreeWidget()
        self.effective_history.setObjectName("effectiveGuidanceHistory")
        self.effective_history.setHeaderLabels(
            ["Revision", "State", "Notification", "Provenance"]
        )
        self.effective_history.setRootIsDecorated(False)
        self.effective_history.setMinimumHeight(110)
        self.starvation_label = QtWidgets.QLabel("No starvation findings")
        self.starvation_label.setObjectName("starvationFindings")
        self.starvation_label.setWordWrap(True)
        self.auth_label = QtWidgets.QLabel("OS session: verifying")
        self.auth_label.setObjectName("guidanceOsSessionState")
        self.auth_label.setWordWrap(True)
        self.recovery_label = QtWidgets.QLabel("Recovery: ready")
        self.recovery_label.setObjectName("guidanceRecoveryState")
        self.recovery_label.setWordWrap(True)
        self.health_details = QtWidgets.QPlainTextEdit()
        self.health_details.setObjectName("guidanceHealthState")
        self.health_details.setReadOnly(True)
        self.health_details.setMaximumHeight(118)
        self.health_details.setPlaceholderText("Guidance service health")
        self.retention_details = QtWidgets.QPlainTextEdit()
        self.retention_details.setObjectName("guidanceRetentionState")
        self.retention_details.setReadOnly(True)
        self.retention_details.setMaximumHeight(92)
        self.retention_details.setPlaceholderText("Retention deadlines and holds")
        status_actions = QtWidgets.QHBoxLayout()
        self.inspect_button = self._button(
            "Refresh",
            QtWidgets.QStyle.StandardPixmap.SP_BrowserReload,
            self.inspect_session,
        )
        self.health_button = self._button(
            "Health",
            QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon,
            self.inspect_health,
        )
        self.running_button = self._button(
            "Running",
            QtWidgets.QStyle.StandardPixmap.SP_FileDialogDetailedView,
            self.inspect_running,
        )
        self.boundary_button = self._button(
            "Boundary",
            QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton,
            self.inspect_boundary,
        )
        self.context_button = self._button(
            "Master context",
            QtWidgets.QStyle.StandardPixmap.SP_MessageBoxInformation,
            self.inspect_context,
        )
        self.refresh_button = self._button(
            "Refresh snapshot",
            QtWidgets.QStyle.StandardPixmap.SP_BrowserReload,
            self.refresh_snapshot,
        )
        self.reconcile_button = self._button(
            "Reconcile",
            QtWidgets.QStyle.StandardPixmap.SP_DialogResetButton,
            self.reconcile_guidance,
        )
        self.effective_button = self._button(
            "History",
            QtWidgets.QStyle.StandardPixmap.SP_FileDialogInfoView,
            self.inspect_effective_history,
        )
        self.cancel_button = self._button(
            "Cancel pending",
            QtWidgets.QStyle.StandardPixmap.SP_DialogCancelButton,
            self.cancel_pending_guidance,
        )
        self.confirm_button = self._button(
            "Confirm",
            QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton,
            self.confirm_prepared,
        )
        self.close_button = self._button(
            "Close",
            QtWidgets.QStyle.StandardPixmap.SP_DialogCloseButton,
            self.close_session,
        )
        self.recover_button = self._button(
            "Recover",
            QtWidgets.QStyle.StandardPixmap.SP_DialogResetButton,
            self.recover_guidance,
        )
        self.disable_button = self._button(
            "Disable",
            QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning,
            self.disable_guidance,
        )
        status_actions.addWidget(self.health_button)
        status_actions.addWidget(self.inspect_button)
        status_actions.addWidget(self.running_button)
        status_actions.addWidget(self.boundary_button)
        status_actions.addWidget(self.context_button)
        status_actions.addWidget(self.refresh_button)
        status_actions.addWidget(self.reconcile_button)
        status_actions.addWidget(self.effective_button)
        status_actions.addStretch(1)
        status_actions.addWidget(self.recover_button)
        status_actions.addWidget(self.confirm_button)
        status_actions.addWidget(self.cancel_button)
        status_actions.addWidget(self.close_button)
        status_actions.addWidget(self.disable_button)
        status_layout.addWidget(self.state_table, 1)
        status_layout.addWidget(self.preview, 3)
        status_layout.addWidget(self.resource_details, 1)
        status_layout.addWidget(self.auth_label)
        status_layout.addWidget(self.recovery_label)
        status_layout.addWidget(self.health_details)
        status_layout.addWidget(self.retention_details)
        status_layout.addWidget(self.starvation_label)
        status_layout.addWidget(self.effective_history, 1)
        status_layout.addWidget(self.timeline, 1)
        status_layout.addLayout(status_actions)
        splitter.addWidget(status_panel)
        splitter.setSizes([760, 400])

        self.status_label = QtWidgets.QLabel("Ready")
        self.status_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        root_layout.addWidget(self.status_label)

    def _button(
        self,
        text: str,
        icon: QtWidgets.QStyle.StandardPixmap,
        callback: Callable[[], None],
    ) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(text)
        button.setIcon(self.style().standardIcon(icon))
        button.clicked.connect(callback)
        return button

    def _load_templates(self) -> None:
        self._templates = [None]
        self.template_combo.clear()
        self.template_combo.addItem("Custom")
        template_root = self._root / "auto-tride-agent" / "guidance-templates"
        for path in sorted(template_root.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict) or not value.get("enabled", False):
                continue
            self._templates.append(value)
            self.template_combo.addItem(str(value.get("label") or value.get("template_id")))
        self.template_combo.currentIndexChanged.connect(self._apply_template)

    def _apply_template(self, index: int) -> None:
        if index <= 0 or index >= len(self._templates):
            return
        template = self._templates[index]
        if template is None:
            return
        body = template.get("body", template.get("content", ""))
        if isinstance(body, str):
            self.content_mode.setCurrentText("Text")
            self.turn_editor.setPlainText(body)
        else:
            self.content_mode.setCurrentText("JSON")
            self.turn_editor.setPlainText(
                json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
            )

    def reset_session(self) -> None:
        self._revision = 0
        self._draft_revision = 0
        self._prepared = None
        self._prepared_preview = None
        self._session_open = False
        self.session_edit.setText(f"side-{uuid4().hex[:10]}")
        self.transcript.clear()
        self.session_list.clear()
        self.timeline.clear()
        self._timeline_entries.clear()
        self._timeline_serial = 0
        self.resource_details.clear()
        self.auth_label.setText("OS session: verifying")
        self.recovery_label.setText("Recovery: ready")
        self.retention_details.clear()
        self.starvation_label.setText("No starvation findings")
        self.effective_history.clear()
        self._latest_notification = None
        self.unsent_label.setVisible(False)
        self._transcript_cursor = None
        self._transcript_next_cursor = None
        self._transcript_page = 0
        self.transcript_page_label.setText("Page 1")
        self.preview.clear()
        self._set_state("Session", "-")
        self._set_state("Revision", "0")
        self._set_state("Draft", "-")
        self._set_state("Delivery", "-")
        self._set_state("Gate", "-")
        self._set_state("Effective", "-")
        self._set_state("Snapshot", "-")
        self._set_session_actions(False)

    def open_session(self) -> None:
        mission_id, session_id = self._identities()
        if mission_id is None or session_id is None:
            return
        self._submit("open", {}, mission_id, session_id)

    def send_turn(self) -> None:
        content = self._editor_content()
        if content is None:
            return
        self._append_transcript("Operator", content["body"])
        self.unsent_label.setVisible(True)
        self._submit("turn", {"content": content})

    def prepare_draft(self) -> None:
        content = self._editor_content()
        if content is None:
            return
        self._prepared = content
        self._prepared_preview = None
        self.preview.setPlainText(_visible_body(content))
        self._submit(
            "prepare",
            {"approved_content": content, "expected_draft_revision": self._draft_revision},
        )

    def confirm_prepared(self) -> None:
        if self._prepared is None or self._draft_revision <= 0:
            self._show_error("No prepared draft")
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Confirm exact guidance")
        dialog.resize(640, 480)
        layout = QtWidgets.QVBoxLayout(dialog)
        stale = bool(self._prepared_preview and self._prepared_preview.get("stale_snapshot_warning"))
        if stale:
            warning = QtWidgets.QLabel("Source snapshot is stale; the main Master will re-evaluate against current state.")
            warning.setObjectName("staleSnapshotWarning")
            warning.setWordWrap(True)
            layout.addWidget(warning)
        exact = QtWidgets.QPlainTextEdit(_visible_body(self._prepared))
        exact.setObjectName("approvedGuidancePreview")
        exact.setReadOnly(True)
        digest_label = QtWidgets.QLabel(self._prepared["body_digest"])
        digest_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(exact, 1)
        layout.addWidget(digest_label)
        interpretation = None if self._prepared_preview is None else self._prepared_preview.get("interpretation")
        if isinstance(interpretation, dict):
            interpretation_label = QtWidgets.QLabel("Side Master interpretation")
            interpretation_view = QtWidgets.QPlainTextEdit(_visible_body(interpretation))
            interpretation_view.setObjectName("sideInterpretationPreview")
            interpretation_view.setReadOnly(True)
            layout.addWidget(interpretation_label)
            layout.addWidget(interpretation_view, 1)
            layout.addWidget(QtWidgets.QLabel(str(interpretation.get("body_digest", ""))))
        layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._confirm_send()

    def _confirm_send(self) -> None:
        if self._prepared is None:
            return
        now_ms = time_ns() // 1_000_000
        self._record_timeline(
            "exact_confirmation",
            now_ms,
            {"body_digest": self._prepared.get("body_digest"), "draft_revision": self._draft_revision},
            identity=f"draft:{self.session_edit.text().strip()}:{self._draft_revision}:exact_confirmation",
        )
        self._submit(
            "send",
            {
                "draft_id": f"draft:{self.session_edit.text().strip()}",
                "expected_draft_revision": self._draft_revision,
                "approved_body_digest": self._prepared["body_digest"],
                "expires_at_ms": now_ms + 24 * 60 * 60 * 1_000,
            },
        )

    def inspect_session(self) -> None:
        self._submit("inspect", {"cursor": self._transcript_cursor, "limit": 32})

    def inspect_health(self) -> None:
        self._submit("health", {})

    def inspect_running(self) -> None:
        self._submit("running", {})

    def inspect_boundary(self) -> None:
        self._submit("boundary", {})

    def inspect_context(self) -> None:
        self._submit("context", {})

    def refresh_snapshot(self) -> None:
        self._submit("refresh", {})

    def reconcile_guidance(self) -> None:
        relation = self.relation_mode.currentText()
        target = self.relation_target.text().strip()
        notification_id = None
        if isinstance(self._latest_notification, dict):
            notification_id = self._latest_notification.get("notification_id")
        self._submit(
            "reconcile",
            {
                "mission_terminal": False,
                "externally_visible": False,
                "irreversible": False,
                "relation": relation,
                "explicit_target": bool(target),
                "target_notification_ids": [target] if target else [],
                "notification_id": notification_id,
                "material_conflict": relation == "conflict",
                "compatible_fragment": False,
            },
        )

    def inspect_effective_history(self) -> None:
        self._submit("effective", {})

    def cancel_pending_guidance(self) -> None:
        notification = self._latest_notification
        if not isinstance(notification, dict):
            self._show_error("No pending guidance is available to cancel")
            return
        state = str(notification.get("state", "pending"))
        if state != "pending":
            self._show_error("The latest guidance is no longer cancellable")
            return
        notification_id = notification.get("notification_id")
        notification_digest = notification.get("notification_digest")
        state_revision = notification.get("state_revision")
        if (
            not isinstance(notification_id, str)
            or not isinstance(notification_digest, str)
            or not isinstance(state_revision, int)
            or isinstance(state_revision, bool)
        ):
            self._show_error("Pending guidance identity is incomplete")
            return
        self._submit(
            "cancel",
            {
                "notification_id": notification_id,
                "notification_digest": notification_digest,
                "expected_state_revision": state_revision,
            },
        )

    def previous_transcript_page(self) -> None:
        if self._transcript_page <= 0:
            return
        self._transcript_page -= 1
        self.transcript_page_label.setText(f"Page {self._transcript_page + 1}")
        self._submit("inspect", {"cursor": None, "limit": 32, "page": self._transcript_page})

    def next_transcript_page(self) -> None:
        self._transcript_page += 1
        self.transcript_page_label.setText(f"Page {self._transcript_page + 1}")
        self._submit("inspect", {"cursor": self._transcript_next_cursor, "limit": 32, "page": self._transcript_page})

    def close_session(self) -> None:
        self._submit("close", {})

    def recover_guidance(self) -> None:
        self._submit("recover", {"recovery_action": "mark_blocked"})

    def disable_guidance(self) -> None:
        self._submit(
            "disable",
            {
                "expected_policy_revision": self._policy_revision,
                "reason": "operator_requested",
            },
        )

    def _submit(
        self,
        action: str,
        payload: Mapping[str, Any],
        mission_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        mission = mission_id or self.mission_edit.text().strip()
        session = session_id or self.session_edit.text().strip()
        try:
            request = self._provider.build_request(
                action,
                payload=payload,
                mission_id=mission,
                session_id=session,
                expected_revision=self._revision,
            )
        except (OSError, PermissionError, ValueError) as error:
            self._show_error(str(error))
            return
        self._set_busy(True)
        self.status_label.setText(f"{action}: pending")
        self._runner.submit(action, request, lambda response: self._handle(action, response))

    def _handle(self, action: str, response: dict[str, Any]) -> None:
        self._set_busy(False)
        status = response.get("status")
        result = response.get("result")
        if not isinstance(result, dict):
            result = {}
        self._update_permitted_actions(response)
        self._render_operational_health(response, result)
        if status not in {"ok", "pending", "cancelled"}:
            self._record_error_timeline(action, response)
            error = response.get("error")
            code = error.get("code") if isinstance(error, dict) else response.get("error_code")
            message = error.get("message") if isinstance(error, dict) else response.get("error_code")
            if code in {"EXPIRED", "BLOCKED", "UNCERTAIN_OUTCOME", "FORBIDDEN"}:
                self._set_state("Delivery", str(code).lower())
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; "
                f"command {str(code or status).lower()}"
            )
            self._render_health_details()
            self._show_error(str(message or "guidance command failed"))
            self._set_session_actions(self._session_open)
            return
        revision = response.get("authoritative_revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            self._revision = revision
            self._set_state("Revision", str(revision))
        if action == "health":
            health = response.get("health")
            if isinstance(health, Mapping):
                source_revision = health.get("source_revision")
                if isinstance(source_revision, int) and not isinstance(source_revision, bool):
                    self._policy_revision = source_revision
        self._ingest_timeline(action, response, result)
        if action == "open":
            self._session_open = True
            self._set_state("Session", self.session_edit.text().strip())
            self._set_session_actions(True)
            self.unsent_label.setVisible(False)
        elif action == "prepare":
            draft_revision = result.get("draft_revision")
            if isinstance(draft_revision, int):
                self._draft_revision = draft_revision
                self._set_state("Draft", str(draft_revision))
            preview = result.get("preview")
            if isinstance(preview, dict) and isinstance(preview.get("approved_content"), dict):
                self._prepared_preview = preview
                self._prepared = preview["approved_content"]
                visible = ["Approved content", _visible_body(self._prepared)]
                interpretation = preview.get("interpretation")
                if isinstance(interpretation, dict):
                    visible.extend(["", "Side Master interpretation", _visible_body(interpretation)])
                if preview.get("stale_snapshot_warning"):
                    visible.extend(["", "STALE SNAPSHOT"])
                self.preview.setPlainText("\n".join(visible))
                self._set_state(
                    "Snapshot",
                    f"{preview.get('source_mission_revision', 0)} -> {preview.get('current_mission_revision', 0)}",
                )
                self._set_state("Effective", str(preview.get("current_effective_revision", 0)))
        elif action == "send":
            self._set_state("Delivery", str(result.get("delivery_state", "pending")))
            confirmation = result.get("confirmation")
            if isinstance(confirmation, dict):
                self._latest_notification = {
                    "notification_id": confirmation.get("notification_id"),
                    "notification_digest": confirmation.get("notification_digest"),
                    "state": "pending",
                    "state_revision": 1,
                }
            self.confirm_button.setEnabled(False)
            self.unsent_label.setVisible(False)
        elif action == "cancel":
            cancellation = result.get("cancellation")
            if isinstance(cancellation, dict):
                self._latest_notification = {
                    **(self._latest_notification or {}),
                    "state": cancellation.get("state", "cancelled"),
                    "state_revision": cancellation.get("state_revision", 0),
                }
                self._set_state("Delivery", str(cancellation.get("state", "cancelled")))
                self._record_timeline(
                    "cancelled",
                    response.get("completed_at_ms"),
                    cancellation,
                    identity=f"notification:{cancellation.get('notification_id', '-')}:cancelled",
                )
        elif action == "close":
            self._session_open = False
            self._render_retention(result)
            self._set_session_actions(False)
        elif action == "health":
            pass
        elif action == "recover":
            recovery = result.get("recovery")
            if isinstance(recovery, Mapping):
                self._render_recovery(recovery)
                self._record_timeline(
                    "recovery",
                    response.get("completed_at_ms"),
                    recovery,
                    identity=f"recovery:{recovery.get('operation_id', self._revision)}",
                )
        elif action == "disable":
            policy = result.get("policy")
            if isinstance(policy, Mapping):
                policy_revision = policy.get("revision")
                if isinstance(policy_revision, int) and not isinstance(policy_revision, bool):
                    self._policy_revision = policy_revision
            self._guidance_enabled = False
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; disabled"
            )
            self._render_health_details()
            self._set_session_actions(self._session_open)
        elif action == "inspect":
            self._update_running_projection(result)
            projection_pages = result.get("projection_pages")
            if isinstance(projection_pages, list):
                self._apply_projection_pages(projection_pages)
            session = result.get("session")
            if isinstance(session, dict):
                self._set_state("Draft", str(session.get("draft_revision") or "-"))
                gate = session.get("initial_guidance_gate")
                if isinstance(gate, dict):
                    transitions = gate.get("transitions")
                    if isinstance(transitions, list) and transitions:
                        current = transitions[-1]
                        if isinstance(current, dict):
                            self._set_state("Gate", str(current.get("state", "-")))
                self._set_state(
                    "Effective", str(session.get("effective_guidance_revision", 0))
                )
                snapshot = session.get("snapshot_digest")
                if isinstance(snapshot, str) and snapshot:
                    self._set_state("Snapshot", snapshot[:18])
        elif action in {"running", "boundary", "context"}:
            self._render_running_state(action, result)
        elif action == "reconcile":
            decision = result.get("decision")
            conflict = result.get("conflict")
            notification_history = result.get("notification_history")
            self.preview.setPlainText(
                "Reconciliation\n"
                + json.dumps(
                    {
                        "decision": decision,
                        "conflict": conflict,
                        "notification_history": notification_history,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            if isinstance(decision, dict):
                self._record_timeline(
                    "reconciliation",
                    response.get("completed_at_ms"),
                    decision,
                    identity=f"reconciliation:{decision.get('source_snapshot_id', '-')}",
                )
        elif action == "effective":
            history = result.get("history")
            if isinstance(history, dict):
                self._render_effective_history(history)
        usage = response.get("usage")
        if isinstance(usage, dict) and isinstance(usage.get("remaining"), dict):
            remaining = usage["remaining"]
            self._set_state(
                "Resources",
                f"in {remaining.get('input_tokens', 0)} / out {remaining.get('output_tokens', 0)}",
            )
        self.status_label.setText(f"{action}: {status}")

    def _render_running_state(self, action: str, result: Mapping[str, Any]) -> None:
        """Render read-only running guidance without copying the transcript."""
        running = result.get("running")
        if isinstance(running, dict):
            notification = running.get("notification")
            if isinstance(notification, dict):
                self._latest_notification = dict(notification)
                self._set_state("Delivery", str(notification.get("state", "confirmed")))
            boundary = running.get("boundary")
            if isinstance(boundary, dict):
                self._append_timeline(
                    "boundary",
                    boundary.get("boundary_id", "-"),
                    boundary,
                )
            interpretation = running.get("interpretation")
            if isinstance(interpretation, dict):
                self.preview.setPlainText(
                    "Running interpretation\n"
                    + json.dumps(interpretation, ensure_ascii=False, indent=2, sort_keys=True)
                )
            self._render_resources(running.get("resources", {}))
        boundary = result.get("boundary")
        if isinstance(boundary, dict):
            self._append_timeline("boundary", boundary.get("boundary_id", "-"), boundary)
        context = result.get("master_context")
        if isinstance(context, dict):
            self.preview.setPlainText(
                "Master context\n"
                + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
            )
            guidance = context.get("guidance")
            if isinstance(guidance, dict):
                self._render_resources(guidance.get("resources", {}))
        self.status_label.setText(f"{action}: read-only")

    def _render_effective_history(self, history: Mapping[str, Any]) -> None:
        self.effective_history.clear()
        revisions = history.get("revisions")
        if not isinstance(revisions, list):
            return
        active = set(history.get("active_notification_ids", []))
        for revision in revisions:
            if not isinstance(revision, dict):
                continue
            trigger = str(revision.get("trigger_notification_id", "-"))
            state = "active" if trigger in active else (
                "deferred" if trigger in revision.get("deferred_member_ids", []) else "historical"
            )
            item = QtWidgets.QTreeWidgetItem(
                [
                    str(revision.get("revision", "-")),
                    state,
                    trigger,
                    str(revision.get("ack_id", "-")),
                ]
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, dict(revision))
            self.effective_history.addTopLevelItem(item)
        if revisions:
            latest = revisions[-1]
            if isinstance(latest, dict):
                self._set_state("Effective", str(latest.get("revision", 0)))

    def _update_running_projection(self, result: Mapping[str, Any]) -> None:
        session = result.get("session")
        if isinstance(session, dict):
            sessions = result.get("sessions")
            if isinstance(sessions, list):
                self.session_list.clear()
                for item in sessions:
                    if isinstance(item, dict):
                        label = f"{item.get('session_id', '-')} | {item.get('state', 'unknown')}"
                        self.session_list.addItem(label)
            transcript = result.get("transcript")
            turns = transcript.get("turns") if isinstance(transcript, dict) else session.get("transcript")
            if isinstance(turns, list):
                self.transcript.clear()
                for turn in turns:
                    if isinstance(turn, dict):
                        speaker = str(turn.get("speaker", "unknown"))
                        body = turn.get("content", turn.get("body", ""))
                        self._append_transcript(speaker, body)
                self.unsent_label.setVisible(bool(turns) and not bool(session.get("published", False)))
            if isinstance(transcript, dict):
                self._transcript_cursor = transcript.get("cursor") if isinstance(transcript.get("cursor"), str) else None
                self._transcript_next_cursor = transcript.get("next_cursor") if isinstance(transcript.get("next_cursor"), str) else None
            else:
                self._transcript_cursor = session.get("next_cursor") if isinstance(session.get("next_cursor"), str) else None
        resources = result.get("resources")
        if isinstance(resources, dict):
            self._render_resources(resources)
        starvation = result.get("starvation")
        if isinstance(starvation, list):
            blocking = [item for item in starvation if isinstance(item, dict) and item.get("blocking")]
            self.starvation_label.setText(
                "Required Agent starvation: " + str(len(blocking)) if blocking else "No starvation findings"
            )
        self._render_auth(result.get("auth"))
        self._render_recovery(result.get("recovery"))
        self._render_retention(result.get("retention"))
        timeline = result.get("timeline")
        if isinstance(timeline, list):
            for item in timeline:
                if isinstance(item, dict):
                    state = item.get("state", "unknown")
                    self._record_timeline(
                        str(state),
                        item.get("at_ms", item.get("created_at_ms")),
                        item,
                        identity=self._timeline_identity(str(state), item),
                    )

    def _update_permitted_actions(self, response: Mapping[str, Any]) -> None:
        actions = response.get("permitted_next_actions")
        if isinstance(actions, list) and all(
            isinstance(action, str) and action for action in actions
        ):
            self._permitted_actions = set(actions)

    def _render_operational_health(
        self,
        response: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        for name in (
            "queue",
            "route",
            "bridge",
            "projection",
            "resources",
            "retention",
            "recovery",
            "auth",
            "boundary",
        ):
            value = result.get(name)
            if isinstance(value, Mapping):
                self._health_sections[name] = self._health_safe_detail(value)
        health = response.get("health")
        if isinstance(health, Mapping):
            self._health_sections["health"] = self._health_safe_detail(health)
        recovery = result.get("recovery")
        if isinstance(recovery, Mapping):
            self._render_recovery(recovery)
        auth = result.get("auth")
        if isinstance(auth, Mapping):
            self._render_auth(auth)
        resources = result.get("resources")
        if isinstance(resources, Mapping):
            self._render_resources(resources)
        retention = result.get("retention")
        if isinstance(retention, Mapping):
            self._render_retention(retention)
        self._render_health_details()

    def _render_health_details(self) -> None:
        rendered = json.dumps(
            self._health_sections,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        self.health_details.setPlainText(
            f"Projection: {self._projection_status}\n{rendered}" if rendered else (
                f"Projection: {self._projection_status}"
            )
        )

    @classmethod
    def _health_safe_detail(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._health_safe_detail(item)
                for key, item in value.items()
                if not any(
                    part in str(key).lower()
                    for part in (
                        "authorization",
                        "credential",
                        "api_key",
                        "secret",
                        "token",
                        "prompt",
                        "body",
                        "content",
                    )
                )
            }
        if isinstance(value, list):
            return [cls._health_safe_detail(item) for item in value]
        return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)

    def _render_auth(self, auth: Any) -> None:
        if not isinstance(auth, Mapping):
            return
        state = str(auth.get("state", "unavailable"))
        epoch = auth.get("verification_epoch", 0)
        revocation = auth.get("revocation_revision", 0)
        self.auth_label.setText(
            f"OS session: {state} (epoch {epoch}, revocation {revocation})"
        )
        self._auth_writable = state == "active"
        if not self._auth_writable:
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; "
                f"OS session {state}"
            )
            self._render_health_details()
        self._set_session_actions(self._session_open)
        self.open_button.setEnabled(
            not self._session_open
            and self._auth_writable
            and self._guidance_enabled
            and self._action_permitted("open")
        )

    def _render_recovery(self, recovery: Any) -> None:
        if not isinstance(recovery, Mapping):
            return
        state = str(recovery.get("state", "unknown"))
        visible = recovery.get("state_visible_elapsed_ms")
        resumed = recovery.get(
            "resume_elapsed_ms",
            recovery.get("resume_or_block_elapsed_ms"),
        )
        checkpoint = recovery.get("checkpoint_age_ms")
        self.recovery_label.setText(
            f"Recovery: {state} | checkpoint {checkpoint} ms | visible {visible} ms | resume {resumed} ms"
        )

    def _render_retention(self, retention: Any) -> None:
        records: list[Mapping[str, Any]]
        if isinstance(retention, list):
            records = [item for item in retention if isinstance(item, Mapping)]
        elif isinstance(retention, Mapping):
            nested = retention.get("records")
            records = (
                [item for item in nested if isinstance(item, Mapping)]
                if isinstance(nested, list)
                else [retention]
            )
        else:
            return
        lines = []
        for record in records:
            lines.append(
                " | ".join(
                    [
                        str(record.get("retention_id", record.get("subject_id", "-"))),
                        str(record.get("state", "unknown")),
                        f"delete_after_ms={record.get('delete_after_ms')}",
                        f"basis={record.get('basis', '-')}",
                    ]
                )
            )
        self.retention_details.setPlainText("\n".join(lines))

    def _render_resources(self, resources: Mapping[str, Any]) -> None:
        labels = ("ceiling", "allocated", "protected", "reserved", "consumed", "remaining", "burn_rate", "projected_usage")
        lines = [f"{label}: {resources.get(label, {})}" for label in labels]
        lines.append(f"forecast_horizon_ms: {resources.get('forecast_horizon_ms', 0)}")
        self.resource_details.setPlainText("\n".join(lines))
        remaining = resources.get("remaining")
        if isinstance(remaining, dict):
            self._set_state("Resources", f"in {remaining.get('input_tokens', 0)} / out {remaining.get('output_tokens', 0)}")

    def _append_timeline(self, state: str, at: Any, detail: Any = None) -> None:
        self._record_timeline(state, at, detail)

    def _record_timeline(
        self,
        state: str,
        at: Any = None,
        detail: Any = None,
        *,
        identity: str | None = None,
    ) -> None:
        """Store one redacted timeline event and render it deterministically.

        Events are keyed by their source identity and state, so projection refreshes and
        duplicate transport reads cannot append another copy of the same event.
        """
        state = str(state or "unknown")
        safe_detail = self._timeline_safe_detail(detail)
        event_identity = identity or self._timeline_identity(state, detail, at)
        timestamp = self._timeline_timestamp(at, detail)
        existing = self._timeline_entries.get(event_identity)
        if existing is None:
            self._timeline_serial += 1
            self._timeline_entries[event_identity] = {
                "identity": event_identity,
                "state": state,
                "at_ms": timestamp,
                "sort": self._timeline_serial,
                "detail": safe_detail,
            }
        else:
            if timestamp is not None and (
                existing.get("at_ms") is None or timestamp < existing["at_ms"]
            ):
                existing["at_ms"] = timestamp
            if safe_detail is not None:
                existing["detail"] = safe_detail
        self._render_timeline()

    def _render_timeline(self) -> None:
        entries = sorted(
            self._timeline_entries.values(),
            key=lambda item: (
                item["at_ms"] is None,
                item["at_ms"] if item["at_ms"] is not None else 0,
                item["sort"],
                item["identity"],
            ),
        )
        lines: list[str] = []
        for entry in entries:
            at = entry["at_ms"] if entry["at_ms"] is not None else "-"
            line = f"{entry['state']}: {at}"
            if entry["detail"] is not None:
                line += " | " + json.dumps(
                    entry["detail"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            lines.append(line)
        self.timeline.setPlainText("\n".join(lines))

    def _ingest_timeline(
        self, action: str, response: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        """Collect transport, semantic, lifecycle, snapshot and recovery outcomes."""
        if action == "send":
            transport_state = result.get("delivery_state", result.get("transport_state", response.get("status")))
            self._record_timeline(
                "transport_receipt",
                result.get("at_ms", result.get("created_at_ms")),
                {"state": transport_state, **result},
                identity=self._timeline_identity("transport_receipt", result, transport_state),
            )
        notification = result.get("notification")
        if isinstance(notification, Mapping):
            state = notification.get("state")
            if state:
                self._record_timeline(
                    str(state),
                    notification.get("at_ms", notification.get("created_at_ms")),
                    notification,
                    identity=self._timeline_identity(str(state), notification),
                )
        acknowledgement = result.get("acknowledgement")
        if isinstance(acknowledgement, Mapping):
            disposition = acknowledgement.get("disposition", acknowledgement.get("state"))
            if disposition:
                self._record_timeline(
                    "semantic_acknowledgement",
                    acknowledgement.get("acknowledged_at_ms", acknowledgement.get("at_ms")),
                    {"state": disposition, "ack_id": acknowledgement.get("ack_id")},
                    identity=self._timeline_identity("semantic_acknowledgement", acknowledgement),
                )
            denial_codes = acknowledgement.get("policy_denial_codes", acknowledgement.get("policy_denials", []))
            if isinstance(denial_codes, list):
                for code in denial_codes:
                    self._record_timeline(
                        "policy_denial",
                        acknowledgement.get("acknowledged_at_ms", acknowledgement.get("at_ms")),
                        {"code": code, "ack_id": acknowledgement.get("ack_id")},
                        identity=f"ack:{acknowledgement.get('ack_id', '-')}:{code}:policy_denial",
                    )
        boundary = result.get("boundary")
        if isinstance(boundary, Mapping):
            self._record_timeline(
                "safe_boundary",
                boundary.get("checked_at_ms", boundary.get("at_ms")),
                boundary,
                identity=self._timeline_identity("safe_boundary", boundary),
            )
        comparison = result.get("snapshot_comparison")
        if isinstance(comparison, Mapping):
            if comparison.get("stale"):
                self._record_timeline("stale_snapshot", comparison.get("checked_at_ms"), comparison)
            if comparison.get("expired"):
                self._record_timeline("expired", comparison.get("checked_at_ms"), comparison)
        preview = result.get("preview")
        if isinstance(preview, Mapping) and preview.get("stale_snapshot_warning"):
            self._record_timeline("stale_snapshot", preview.get("current_mission_revision"), preview)
        session = result.get("session")
        if isinstance(session, Mapping):
            session_state = session.get("state")
            if session_state in {"closed", "expired", "blocked", "cancelled"}:
                self._record_timeline(str(session_state), session.get("closed_at_ms"), session)
            self._ingest_recovery(session.get("recovery"))
        self._ingest_recovery(result.get("recovery"))
        for key in ("state", "terminal_state", "lifecycle_state"):
            state = result.get(key)
            if isinstance(state, str) and state not in {"ok", "pending", "active", "idle"}:
                self._record_timeline(state, result.get("at_ms"), result)

    def _ingest_recovery(self, recovery: Any) -> None:
        if not isinstance(recovery, Mapping):
            return
        state = recovery.get("state")
        if not isinstance(state, str):
            return
        visible_state = "recovered" if state in {"completed", "ready", "resumed", "state_visible"} else "recovery"
        self._record_timeline(visible_state, recovery.get("at_ms", recovery.get("completed_at_ms")), recovery)

    def _record_error_timeline(self, action: str, response: Mapping[str, Any]) -> None:
        error = response.get("error")
        code = error.get("code") if isinstance(error, Mapping) else response.get("error_code")
        message = error.get("message") if isinstance(error, Mapping) else None
        state = "policy_denial" if str(code).upper() in {"FORBIDDEN", "POLICY_DENIED", "PROTECTED_QUOTA"} else "error"
        self._record_timeline(
            state,
            response.get("at_ms"),
            {"action": action, "code": code, "message": message},
            identity=f"error:{action}:{code or 'unknown'}",
        )

    @staticmethod
    def _timeline_identity(state: str, detail: Any = None, at: Any = None) -> str:
        if isinstance(detail, Mapping):
            for field in (
                "event_id", "notification_id", "ack_id", "boundary_id", "recovery_run_id",
                "run_id", "action_id", "error_code",
            ):
                value = detail.get(field)
                if value not in (None, ""):
                    return f"{field}:{value}:{state}"
        return f"{state}:{at if at is not None else '-'}"

    @staticmethod
    def _timeline_timestamp(at: Any, detail: Any = None) -> int | None:
        if isinstance(at, int) and not isinstance(at, bool):
            return at
        if isinstance(detail, Mapping):
            for field in (
                "at_ms", "created_at_ms", "checked_at_ms", "acknowledged_at_ms", "closed_at_ms",
                "completed_at_ms", "updated_at_ms",
            ):
                value = detail.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
        return None

    @staticmethod
    def _timeline_safe_detail(detail: Any) -> Any:
        if not isinstance(detail, Mapping):
            return detail if isinstance(detail, (str, int, float, bool)) or detail is None else str(detail)
        hidden = {"body", "content", "approved_content", "interpretation", "prompt", "secret", "token"}
        return {
            str(key): value
            for key, value in detail.items()
            if str(key).lower() not in hidden
            and not any(part in str(key).lower() for part in ("credential", "authorization", "api_key"))
        }

    def _apply_projection_pages(self, pages: list[Any]) -> None:
        statuses: list[str] = []
        try:
            for page in pages:
                if not isinstance(page, dict):
                    raise ValueError("invalid guidance projection page")
                statuses.append(self._guidance_state.apply(page))
        except ValueError as error:
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; "
                "rejected projection update"
            )
            self._render_health_details()
            self._show_error(str(error))
            return
        if "applied" in statuses:
            current = self._guidance_state.pages.get(None, {})
            self._projection_status = (
                f"revision {self._guidance_state.projection_revision} "
                f"{current.get('freshness', 'unknown')} / {current.get('certainty', 'unknown')}"
            )
        elif any(status == "stale" for status in statuses):
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; "
                "stale update ignored"
            )
        elif statuses and all(status == "duplicate" for status in statuses):
            self._projection_status = (
                f"last known valid revision {self._guidance_state.projection_revision}; "
                "duplicate update ignored"
            )
        self._render_health_details()
        entities = list(self._guidance_state.entities.values())
        for entity in entities:
            entity_type = str(entity.get("entity_type", ""))
            state = entity.get("state")
            if not isinstance(state, str) or not state:
                continue
            detail = entity.get("display") if isinstance(entity.get("display"), dict) else {}
            detail = {**detail, "entity_type": entity_type, "entity_id": entity.get("entity_id")}
            if entity_type == "notification":
                self._latest_notification = {
                    "notification_id": entity.get("entity_id"),
                    "notification_digest": entity.get("source_digest"),
                    "state": state,
                    "state_revision": entity.get("entity_revision"),
                }
                self._record_timeline(
                    state,
                    detail.get("created_at_ms"),
                    detail,
                    identity=f"notification:{entity.get('entity_id', '-')}:state:{state}",
                )
            elif entity_type == "acknowledgement":
                self._record_timeline(
                    "semantic_acknowledgement",
                    detail.get("acknowledged_at_ms"),
                    {"state": state, **detail},
                    identity=f"ack:{entity.get('entity_id', '-')}:semantic_acknowledgement",
                )
                denial_codes = detail.get("policy_denial_codes", [])
                if isinstance(denial_codes, list):
                    for code in denial_codes:
                        self._record_timeline(
                            "policy_denial",
                            detail.get("acknowledged_at_ms"),
                            {"code": code, "ack_id": entity.get("entity_id")},
                            identity=f"ack:{entity.get('entity_id', '-')}:denial:{code}",
                        )
            elif entity_type == "safe_boundary":
                self._record_timeline(
                    "safe_boundary",
                    detail.get("checked_at_ms"),
                    detail,
                    identity=f"boundary:{entity.get('entity_id', '-')}",
                )
            elif entity_type in {"recovery_checkpoint", "recovery_run"}:
                self._ingest_recovery({"state": state, **detail})
            elif entity_type in {"atomic_action", "health"} and state in {
                "blocked", "cancelled", "expired", "closed", "recovered", "completed",
            }:
                self._record_timeline(
                    state,
                    detail.get("at_ms", detail.get("created_at_ms")),
                    detail,
                    identity=f"{entity_type}:{entity.get('entity_id', '-')}:state:{state}",
                )
        gate = next(
            (
                item
                for item in entities
                if item.get("entity_type") == "health"
                and str(item.get("entity_id", "")).startswith("initial-guidance-gate:")
            ),
            None,
        )
        if gate is not None:
            self._set_state("Gate", str(gate.get("state", "-")))
        effective = next(
            (
                item
                for item in entities
                if item.get("entity_type") == "health"
                and str(item.get("entity_id", "")).startswith("effective-guidance:")
            ),
            None,
        )
        if effective is not None:
            self._set_state("Effective", str(effective.get("entity_revision", 0)))
        projected_members = self._guidance_state.effective_documents
        if projected_members:
            self.effective_history.clear()
            for member in projected_members:
                display = member.get("display") if isinstance(member.get("display"), dict) else {}
                self.effective_history.addTopLevelItem(
                    QtWidgets.QTreeWidgetItem(
                        [
                            str(member.get("entity_revision", "-")),
                            str(member.get("state", "-")),
                            str(member.get("entity_id", "-")),
                            str(display.get("relation_source_id", "-")),
                        ]
                    )
                )
        allocation = next(
            (item for item in entities if item.get("entity_type") == "agent_allocation"),
            None,
        )
        if allocation is not None and isinstance(allocation.get("display"), dict):
            remaining = allocation["display"].get("remaining")
            if isinstance(remaining, dict):
                self._set_state(
                    "Resources",
                    f"in {remaining.get('input_tokens', 0)} / out {remaining.get('output_tokens', 0)}",
                )
        notification = next(
            (item for item in entities if item.get("entity_type") == "notification"),
            None,
        )
        acknowledgement = next(
            (item for item in entities if item.get("entity_type") == "acknowledgement"),
            None,
        )
        if notification is not None and isinstance(notification.get("exact_content"), dict):
            exact = notification["exact_content"]
            approved = exact.get("approved_content")
            if isinstance(approved, dict):
                visible = ["Approved content", _visible_body(approved)]
                interpretation = exact.get("interpretation")
                if isinstance(interpretation, dict):
                    visible.extend(
                        ["", "Side Master interpretation", _visible_body(interpretation)]
                    )
                if acknowledgement is not None and isinstance(
                    acknowledgement.get("exact_content"), dict
                ):
                    ack = acknowledgement["exact_content"]
                    visible.extend(
                        [
                            "",
                            "Master acknowledgement",
                            _visible_body(ack.get("understood_intent", {})),
                            _visible_body(ack.get("expected_effect", {})),
                        ]
                    )
                self.preview.setPlainText("\n".join(visible))

    def _editor_content(self) -> dict[str, Any] | None:
        raw = self.turn_editor.toPlainText()
        if not raw:
            self._show_error("Content is empty")
            return None
        try:
            if self.content_mode.currentText() == "JSON":
                body = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
                exact = json.dumps(
                    body,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                media_type = "application/json"
            else:
                body = raw
                exact = raw.encode("utf-8")
                media_type = "text/plain; charset=utf-8"
        except (ValueError, json.JSONDecodeError) as error:
            self._show_error(f"Invalid JSON: {error}")
            return None
        if len(exact) > MAX_BODY_BYTES:
            self._show_error("Content exceeds 32 KiB")
            return None
        return {
            "media_type": media_type,
            "body": body,
            "canonical_body_base64": b64encode(exact).decode("ascii"),
            "body_digest": f"blake3:{blake3.blake3(exact).hexdigest()}",
        }

    def _identities(self) -> tuple[str | None, str | None]:
        mission, session = self.mission_edit.text().strip(), self.session_edit.text().strip()
        if not mission or not session:
            self._show_error("Mission ID and Session ID are required")
            return None, None
        return mission, session

    def _append_transcript(self, speaker: str, body: Any) -> None:
        visible = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2)
        self.transcript.appendPlainText(f"{speaker}\n{visible}\n")

    def _set_state(self, name: str, value: str) -> None:
        for row in range(self.state_table.rowCount()):
            if self.state_table.item(row, 0).text() == name:
                self.state_table.item(row, 1).setText(value)
                return

    def _set_session_actions(self, enabled: bool) -> None:
        writable = enabled and self._auth_writable and self._guidance_enabled
        self.health_button.setEnabled(self._action_permitted("health"))
        self.turn_button.setEnabled(writable and self._action_permitted("turn"))
        self.prepare_button.setEnabled(writable and self._action_permitted("prepare"))
        self.inspect_button.setEnabled(enabled and self._action_permitted("inspect"))
        self.running_button.setEnabled(enabled and self._action_permitted("running"))
        self.boundary_button.setEnabled(enabled and self._action_permitted("boundary"))
        self.context_button.setEnabled(enabled and self._action_permitted("context"))
        self.refresh_button.setEnabled(writable and self._action_permitted("refresh"))
        self.reconcile_button.setEnabled(writable and self._action_permitted("reconcile"))
        self.effective_button.setEnabled(enabled and self._action_permitted("effective"))
        self.recover_button.setEnabled(
            enabled and self._auth_writable and self._action_permitted("recover")
        )
        self.cancel_button.setEnabled(
            writable and bool(self._latest_notification) and self._action_permitted("cancel")
        )
        self.close_button.setEnabled(enabled and self._action_permitted("close"))
        self.disable_button.setEnabled(
            self._auth_writable
            and self._guidance_enabled
            and self._action_permitted("disable")
        )
        self.confirm_button.setEnabled(
            writable and self._prepared is not None and self._action_permitted("send")
        )

    def _action_permitted(self, action: str) -> bool:
        return not self._permitted_actions or action in self._permitted_actions

    def _set_busy(self, busy: bool) -> None:
        self.open_button.setEnabled(
            not busy
            and not self._session_open
            and self._auth_writable
            and self._guidance_enabled
            and self._action_permitted("open")
        )
        self.new_button.setEnabled(not busy)
        self.health_button.setEnabled(not busy and self._action_permitted("health"))
        if self._session_open:
            self._set_session_actions(not busy)

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)


class LiveValidationQtWidget(QtWidgets.QWidget):
    """Read-only live-validation projection surface."""

    state_received = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = LiveValidationViewState()
        self.state_received.connect(self._apply_state)

        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        self.kind_combo = QtWidgets.QComboBox()
        self.kind_combo.setObjectName("liveValidationKind")
        self.kind_combo.addItems(
            [
                "campaign",
                "route",
                "case",
                "call",
                "budget",
                "tikhub_provenance",
                "scorecard",
                "audit",
                "failure",
                "improvement",
                "final",
            ]
        )
        self.query_edit = QtWidgets.QLineEdit()
        self.query_edit.setObjectName("liveValidationFilter")
        self.query_edit.setPlaceholderText("Filter")
        self.previous_button = self._icon_button(
            QtWidgets.QStyle.StandardPixmap.SP_ArrowLeft, "Previous page"
        )
        self.previous_button.setObjectName("liveValidationPrevious")
        self.next_button = self._icon_button(
            QtWidgets.QStyle.StandardPixmap.SP_ArrowRight, "Next page"
        )
        self.next_button.setObjectName("liveValidationNext")
        self.page_spin = QtWidgets.QSpinBox()
        self.page_spin.setObjectName("liveValidationPage")
        self.page_spin.setRange(0, 0)
        controls.addWidget(self.kind_combo)
        controls.addWidget(self.query_edit, 1)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.page_spin)
        controls.addWidget(self.next_button)
        layout.addLayout(controls)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("liveValidationStatus")
        self.status_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.status_label)

        self.table = QtWidgets.QTableWidget()
        self.table.setObjectName("liveValidationTable")
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.kind_combo.currentTextChanged.connect(self._selection_changed)
        self.query_edit.textChanged.connect(self._render)
        self.page_spin.valueChanged.connect(self._render)
        self.previous_button.clicked.connect(
            lambda: self.page_spin.setValue(max(0, self.page_spin.value() - 1))
        )
        self.next_button.clicked.connect(
            lambda: self.page_spin.setValue(
                min(self.page_spin.maximum(), self.page_spin.value() + 1)
            )
        )
        self._selection_changed()

    def update_live_validation(self, state: LiveValidationViewState) -> None:
        self.state_received.emit(state)

    @QtCore.Slot(object)
    def apply_event(self, event: object) -> None:
        self.state_received.emit(self._state.apply(event))

    @QtCore.Slot(object)
    def _apply_state(self, state: object) -> None:
        if not isinstance(state, LiveValidationViewState):
            return
        self._state = state
        self._selection_changed()

    def _selection_changed(self) -> None:
        kind = self.kind_combo.currentText()
        pages = [
            page.page_index for page in self._state.pages.values() if page.page_kind == kind
        ]
        maximum = max(pages, default=0)
        self.page_spin.setMaximum(maximum)
        self.page_spin.setValue(min(self.page_spin.value(), maximum))
        self._render()

    def _render(self) -> None:
        kind = self.kind_combo.currentText()
        index = self.page_spin.value()
        query = self.query_edit.text()
        rows = self._state.page(kind, index, query=query)
        columns = sorted({key for row in rows for key in row})
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, key in enumerate(columns):
                value = row.get(key)
                text = (
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                self.table.setItem(row_index, column_index, QtWidgets.QTableWidgetItem(text))
        page = self._state.pages.get(f"{kind}:{index}")
        metadata = "unavailable"
        if page is not None:
            metadata = " / ".join(
                value
                for value in (
                    page.certainty,
                    page.freshness,
                    page.error_code,
                )
                if value
            )
        campaign = self._state.campaign_id or "no-campaign"
        self.status_label.setText(
            f"{campaign} / {kind} / {index + 1} / {metadata} / {len(rows)}"
        )
        self.previous_button.setEnabled(index > 0)
        self.next_button.setEnabled(index < self.page_spin.maximum())

    def _icon_button(
        self, icon: QtWidgets.QStyle.StandardPixmap, tooltip: str
    ) -> QtWidgets.QToolButton:
        button = QtWidgets.QToolButton()
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(tooltip)
        return button


class AgentConsoleWidget(QtWidgets.QWidget):
    """vn.py app surface with Side Master guidance as the primary operational tab."""

    def __init__(self, main_engine: Any, event_engine: Any) -> None:
        super().__init__()
        self.setWindowTitle("Agent Console")
        self.resize(1200, 800)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(SideMasterGuidanceWidget(main_engine, event_engine), "Guidance")
        self.live_validation = LiveValidationQtWidget()
        tabs.addTab(self.live_validation, "Live Validation")
        layout.addWidget(tabs)
        bridge = None
        get_engine = getattr(main_engine, "get_engine", None)
        if callable(get_engine):
            bridge = get_engine("agent_bridge") or get_engine("AgentBridge")
        subscribe = getattr(bridge, "subscribe_live_validation", None)
        if callable(subscribe):
            subscribe(lambda event, _ack: self.live_validation.apply_event(event))


def _agentctl_program(root: Path) -> str:
    configured = os.environ.get("AGENTCTL_PATH")
    if configured:
        return configured
    executable = "agentctl.exe" if os.name == "nt" else "agentctl"
    for profile in ("release", "debug"):
        candidate = root / "auto-tride-rust" / "target" / profile / executable
        if candidate.is_file():
            return str(candidate)
    return executable


def _visible_body(content: Mapping[str, Any]) -> str:
    body = content.get("body")
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _local_error(code: str, message: str) -> dict[str, Any]:
    return {
        "entity_type": "guidance_cli_response",
        "contract_version": 1,
        "status": "blocked",
        "error": {"code": code, "message": message},
    }
