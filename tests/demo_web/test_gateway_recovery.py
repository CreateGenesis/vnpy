from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vnpy.demo_web.gateway_control import GatewayControlError, GatewayControlService

from test_gateway_controls import Configuration, RunClient, Supervisor


class UnresolvedClient(RunClient):
    def drain_shutdown(self, _key: str) -> dict[str, Any]:
        self.drain_calls += 1
        return {
            "gateway": self.gateway,
            "state": "blocked",
            "data": {
                "reconciliation_state": "blocked",
                "unresolved_outcomes": 1,
            },
        }


def test_active_campaign_is_paused_before_gateway_stop(tmp_path: Path) -> None:
    events: list[str] = []
    supervisor = Supervisor()
    supervisor.states["run_xtp"].update(state="ready")
    client = RunClient("XTP")
    original_drain = client.drain_shutdown

    def drain(key: str) -> dict[str, Any]:
        events.append("drain")
        return original_drain(key)

    client.drain_shutdown = drain  # type: ignore[method-assign]
    control = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=lambda _gateway: client,
        active_campaign=lambda: {
            "campaign_id": "b53bc59c-c626-4f16-8a3e-a3185c7dad23",
            "state": "active",
            "gateways": ["XTP"],
        },
        pause_campaign=lambda _campaign_id: events.append("pause") or {"state": "paused"},
    )

    result = control.control(
        "XTP",
        "stop",
        {"expected_revision": 0, "idempotency_key": "stop-active-xtp-gateway"},
    )

    assert result["state"] == "stopped"
    assert events == ["pause", "drain"]


def test_unresolved_outcome_blocks_process_stop(tmp_path: Path) -> None:
    supervisor = Supervisor()
    supervisor.states["run_xtp"].update(state="ready")
    client = UnresolvedClient("XTP")
    control = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=lambda _gateway: client,
    )

    with pytest.raises(GatewayControlError, match="GATEWAY_RECONCILIATION_REQUIRED"):
        control.control(
            "XTP",
            "stop",
            {"expected_revision": 0, "idempotency_key": "stop-unresolved-xtp-01"},
        )

    assert supervisor.calls == []
    assert client.drain_calls == 1


def test_reconnect_recovers_crashed_process_without_campaign_resend(tmp_path: Path) -> None:
    supervisor = Supervisor()
    client = RunClient("XTP")
    available = False

    def load(_gateway: str) -> RunClient | None:
        return client if available else None

    control = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=load,
    )

    def started(command: dict[str, Any], payload: bytes) -> dict[str, Any]:
        nonlocal available
        result = Supervisor.handle_with_secret(supervisor, command, payload)
        available = True
        return result

    supervisor.handle_with_secret = started  # type: ignore[method-assign]
    result = control.control(
        "XTP",
        "reconnect",
        {"expected_revision": 0, "idempotency_key": "recover-crashed-xtp-01"},
    )

    assert result["state"] == "connected"
    assert len(supervisor.secret_calls) == 1
    assert client.reconnect_calls == 0

