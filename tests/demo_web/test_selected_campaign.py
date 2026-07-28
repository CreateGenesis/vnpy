from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from vnpy.demo_web.runtime import ConcreteDemoBackend, DemoCandidate
from vnpy.demo_web.run_clients import RunClientBinding


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class Client:
    def __init__(self, gateway: str) -> None:
        self.binding = RunClientBinding(
            gateway,
            digest(f"run:{gateway}"),
            f"tcp://127.0.0.1:{17801 if gateway == 'XTP' else 17802}",
        )
        self.operations: list[str] = []

    def gateway_health(self) -> dict[str, Any]:
        return {"gateway": self.binding.gateway, "state": "connected", "data": {}}

    def read_status(self) -> dict[str, Any]:
        return {
            "gateway": self.binding.gateway,
            "state": "ready",
            "data": {"connection_state": "connected"},
        }

    def prepare_campaign(self, *_args: Any) -> dict[str, Any]:
        self.operations.append("prepare")
        return {"gateway": self.binding.gateway, "state": "prepared"}

    def start_campaign(self, *_args: Any) -> dict[str, Any]:
        self.operations.append("start")
        return {"gateway": self.binding.gateway, "state": "active"}

    def emergency_stop(self, *_args: Any) -> dict[str, Any]:
        self.operations.append("stop")
        return {"gateway": self.binding.gateway, "state": "stopped"}

    def pause_campaign(self, *_args: Any) -> dict[str, Any]:
        self.operations.append("pause")
        return {"gateway": self.binding.gateway, "state": "paused"}


class GatewayProvider:
    def __init__(self, selected: set[str], clients: dict[str, Client]) -> None:
        self.selected = selected
        self._clients = clients

    def selected_gateways(self) -> set[str]:
        return set(self.selected)

    def clients(self) -> dict[str, Client]:
        return dict(self._clients)


@pytest.mark.parametrize("selected", [{"XTP"}, {"TORA"}, {"XTP", "TORA"}])
def test_campaign_uses_exact_selected_ready_gateway_set(
    tmp_path: Path,
    selected: set[str],
) -> None:
    clients = {gateway: Client(gateway) for gateway in ("XTP", "TORA")}
    backend = ConcreteDemoBackend(
        tmp_path,
        candidate(),
        GatewayProvider(selected, clients),
        guidance_available=False,
    )

    result = backend.start_campaign(
        {
            "candidate_digest": digest("candidate"),
            "idempotency_key": "selected-campaign-start-0001",
        }
    )

    assert result["state"] == "active"
    assert {item["gateway"] for item in result["gateways"]} == selected
    for gateway, client in clients.items():
        assert client.operations == (["prepare", "start"] if gateway in selected else [])


def test_unselected_unavailable_gateway_and_side_master_do_not_block_campaign(
    tmp_path: Path,
) -> None:
    xtp = Client("XTP")
    backend = ConcreteDemoBackend(
        tmp_path,
        candidate(),
        GatewayProvider({"XTP"}, {"XTP": xtp}),
        guidance_available=False,
    )

    readiness = backend.readiness()
    result = backend.start_campaign(
        {
            "candidate_digest": digest("candidate"),
            "idempotency_key": "xtp-only-campaign-start-1",
        }
    )

    assert readiness["ready"] is True
    assert all(item["code"] != "SIDE_MASTER_UNAVAILABLE" for item in readiness["blockers"])
    assert result["state"] == "active"


def test_explicit_gateway_list_must_match_durable_selection(tmp_path: Path) -> None:
    clients = {gateway: Client(gateway) for gateway in ("XTP", "TORA")}
    backend = ConcreteDemoBackend(
        tmp_path,
        candidate(),
        GatewayProvider({"XTP"}, clients),
        guidance_available=True,
    )

    with pytest.raises(RuntimeError, match="SELECTED_GATEWAY_SET_MISMATCH"):
        backend.start_campaign(
            {
                "candidate_digest": digest("candidate"),
                "gateways": ["XTP", "TORA"],
                "idempotency_key": "wrong-selected-campaign-1",
            }
        )


def test_pause_and_emergency_stop_do_not_depend_on_agent_services(tmp_path: Path) -> None:
    xtp = Client("XTP")
    backend = ConcreteDemoBackend(
        tmp_path,
        candidate(),
        GatewayProvider({"XTP"}, {"XTP": xtp}),
        guidance_available=False,
    )
    started = backend.start_campaign(
        {
            "candidate_digest": digest("candidate"),
            "idempotency_key": "containment-campaign-start-1",
        }
    )

    paused = backend.pause_campaign(started["campaign_id"])
    stopped = backend.emergency_stop()

    assert paused["state"] == "paused"
    assert stopped["state"] == "contained"
    assert xtp.operations == ["prepare", "start", "pause", "stop"]


def candidate() -> DemoCandidate:
    return DemoCandidate(
        ready=True,
        candidate_digest=digest("candidate"),
        author_lineage_digest=digest("author"),
        package_digest=digest("package"),
        configuration_digest=digest("configuration"),
        policy_digest=digest("policy"),
        symbols=("600000.SH",),
        calendar_sessions=tuple(date(2026, 7, day) for day in range(27, 32)),
        lifecycle_revision=1,
    )
