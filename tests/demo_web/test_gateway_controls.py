from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from vnpy.demo_web.gateway_control import GatewayControlError, GatewayControlService
from vnpy.demo_web.supervisor import SupervisorError


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class Configuration:
    def read_active(self) -> dict[str, Any]:
        return {
            "state": "active",
            "version": 3,
            "configuration_digest": digest("configuration"),
            "operator_identity_digest": digest("operator"),
            "sections": {
                "xtp": xtp_public(),
                "tora": tora_public(),
            },
        }

    def read_section_secrets(self, section: str) -> dict[str, str]:
        if section == "xtp":
            return {"password": "xtp-password", "authorization_code": "xtp-auth"}
        return {"password": "tora-password", "dynamic_key": "tora-dynamic"}


class Supervisor:
    def __init__(self) -> None:
        self.states = {
            "run_xtp": {"service": "run_xtp", "state": "stopped", "revision": 0},
            "run_tora": {"service": "run_tora", "state": "stopped", "revision": 0},
        }
        self.secret_calls: list[tuple[dict[str, Any], bytes]] = []
        self.calls: list[dict[str, Any]] = []
        self.fail_service: str | None = None

    def reconcile(self, service: Any) -> dict[str, Any]:
        return dict(self.states[service.value])

    def handle_with_secret(
        self,
        command: dict[str, Any],
        secret_payload: bytes,
    ) -> dict[str, Any]:
        if command["service"] == self.fail_service:
            raise SupervisorError("SUPERVISOR_START_FAILED")
        self.secret_calls.append((dict(command), secret_payload))
        state = self.states[command["service"]]
        state.update(state="ready", revision=state["revision"] + 1)
        return dict(state)

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(command))
        state = self.states[command["service"]]
        state.update(state="stopped", revision=state["revision"] + 1)
        return dict(state)


class RunClient:
    def __init__(self, gateway: str) -> None:
        self.gateway = gateway
        self.reconnect_calls = 0
        self.drain_calls = 0

    def gateway_health(self) -> dict[str, Any]:
        return {"gateway": self.gateway, "state": "connected", "data": {}}

    def reconnect(self, _key: str) -> dict[str, Any]:
        self.reconnect_calls += 1
        return {"gateway": self.gateway, "state": "connected", "data": {}}

    def drain_shutdown(self, _key: str) -> dict[str, Any]:
        self.drain_calls += 1
        return {
            "gateway": self.gateway,
            "state": "stopped",
            "data": {"reconciliation_state": "complete"},
        }


def test_gateway_start_select_reconnect_stop_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    supervisor = Supervisor()
    clients = {"XTP": RunClient("XTP"), "TORA": RunClient("TORA")}
    control = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=lambda gateway: clients.get(gateway),
    )

    started = control.control(
        "XTP",
        "start",
        {"expected_revision": 0, "idempotency_key": "start-xtp-gateway-0001"},
    )
    replay = control.control(
        "XTP",
        "start",
        {"expected_revision": 0, "idempotency_key": "start-xtp-gateway-0001"},
    )
    selected = control.control(
        "XTP",
        "select",
        {
            "expected_revision": 1,
            "idempotency_key": "select-xtp-gateway-001",
            "selected": True,
        },
    )
    reconnected = control.control(
        "XTP",
        "reconnect",
        {"expected_revision": 2, "idempotency_key": "reconnect-xtp-gateway-1"},
    )

    assert started == replay
    assert len(supervisor.secret_calls) == 1
    launch = json.loads(supervisor.secret_calls[0][1])
    assert launch["gateway"] == "XTP"
    assert launch["configuration_version"] == 3
    assert launch["secrets"]["password"] == "xtp-password"
    assert "secrets" not in json.dumps(started)
    assert selected["selected"] is True
    assert control.selected_gateways() == {"XTP"}
    assert reconnected["state"] == "connected"
    assert clients["XTP"].reconnect_calls == 1

    stopped = control.control(
        "XTP",
        "stop",
        {"expected_revision": 3, "idempotency_key": "stop-xtp-gateway-00001"},
    )
    assert stopped["state"] == "stopped"
    assert clients["XTP"].drain_calls == 1
    assert supervisor.calls[-1]["service"] == "run_xtp"

    restarted = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=lambda gateway: clients.get(gateway),
    )
    assert restarted.selected_gateways() == {"XTP"}


def test_gateway_failure_does_not_change_other_gateway_state(tmp_path: Path) -> None:
    supervisor = Supervisor()
    clients = {"TORA": RunClient("TORA")}
    control = GatewayControlService(
        tmp_path,
        Configuration(),
        supervisor,
        client_loader=lambda gateway: clients.get(gateway),
    )
    control.control(
        "TORA",
        "start",
        {"expected_revision": 0, "idempotency_key": "start-tora-gateway-001"},
    )
    supervisor.fail_service = "run_xtp"

    with pytest.raises(GatewayControlError, match="SUPERVISOR_START_FAILED"):
        control.control(
            "XTP",
            "start",
            {"expected_revision": 1, "idempotency_key": "start-xtp-gateway-0001"},
        )

    projected = {item["gateway"]: item for item in control.projection()["gateways"]}
    assert projected["TORA"]["state"] == "connected"
    assert projected["XTP"]["state"] in {"stopped", "unavailable"}


def xtp_public() -> dict[str, Any]:
    return {
        "account": "xtp-simulation",
        "client_id": 7,
        "quote_address": "quote.simulation.invalid",
        "quote_port": 10001,
        "trading_address": "trade.simulation.invalid",
        "trading_port": 10002,
        "quote_protocol": "TCP",
        "log_level": "INFO",
    }


def tora_public() -> dict[str, Any]:
    return {
        "account": "tora-simulation",
        "product_id": "demo",
        "account_type": "资金账号",
        "address_type": "前置地址",
        "quote_server": "tcp://quote.simulation.invalid:20001",
        "trading_server": "tcp://trade.simulation.invalid:20002",
    }
