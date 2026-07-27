from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

import pytest

from vnpy.demo_web.controls import DemoCampaignControls
from vnpy.demo_web.run_clients import BrokerSimulationRunClient, RunClientBinding


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class ControlTransport:
    def __init__(self, *, unavailable_gateway: str | None = None) -> None:
        self.unavailable_gateway = unavailable_gateway
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = Lock()

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self.calls.append((endpoint, operation, payload))
        gateway = payload["gateway"]
        if gateway == self.unavailable_gateway:
            raise ConnectionError("run unavailable")
        state = "paused" if operation == "run.pause_campaign.v1" else "stopped"
        return {
            "contract_version": 1,
            "gateway": gateway,
            "run_digest": payload["run_digest"],
            "operation": operation,
            "state": state,
            "receipt_digest": digest(f"{operation}:{gateway}"),
            "data": {
                "hard_stop_deadline_met": True,
                "working_order_count": 0,
                "residual_exposure_minor": 0,
                "unresolved_outcomes": 0,
                "permitted_next_action": "start_new_campaign",
            },
        }


class NoCallTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        raise AssertionError("retained receipt must not redispatch")


def clients(transport: Any) -> tuple[BrokerSimulationRunClient, ...]:
    return (
        BrokerSimulationRunClient(
            RunClientBinding("XTP", digest("run:XTP"), "tcp://127.0.0.1:19701"),
            transport,
        ),
        BrokerSimulationRunClient(
            RunClientBinding("TORA", digest("run:TORA"), "tcp://127.0.0.1:19702"),
            transport,
        ),
    )


def test_pause_works_with_side_master_agentd_and_modeld_unavailable(tmp_path: Path) -> None:
    transport = ControlTransport()
    controls = DemoCampaignControls(
        clients=clients(transport),
        database=tmp_path / "controls.sqlite",
        clock_ns=iter((1_000_000_000, 1_300_000_000)).__next__,
    )

    receipt = controls.pause_campaign(
        campaign_digest=digest("campaign"),
        idempotency_key="pause-request-0001",
    )

    assert receipt["state"] == "paused"
    assert receipt["action"] == "pause"
    assert [item["gateway"] for item in receipt["gateways"]] == ["TORA", "XTP"]
    assert {call[1] for call in transport.calls} == {"run.pause_campaign.v1"}
    assert len(transport.calls) == 2
    assert all("agent" not in str(call).lower() for call in transport.calls)
    assert all("modeld" not in str(call).lower() for call in transport.calls)
    assert all("side_master" not in str(call).lower() for call in transport.calls)


def test_emergency_stop_attempts_every_run_and_fails_closed_if_one_is_unavailable(
    tmp_path: Path,
) -> None:
    transport = ControlTransport(unavailable_gateway="TORA")
    controls = DemoCampaignControls(
        clients=clients(transport),
        database=tmp_path / "controls.sqlite",
        clock_ns=iter((1_000_000_000, 1_400_000_000)).__next__,
    )

    receipt = controls.emergency_stop(idempotency_key="stop-request-000001")

    assert receipt["state"] == "uncertain"
    assert receipt["hard_stop_deadline_met"] is False
    assert len(transport.calls) == 2
    assert {call[1] for call in transport.calls} == {"run.emergency_stop.v1"}
    by_gateway = {item["gateway"]: item for item in receipt["gateways"]}
    assert by_gateway["XTP"]["state"] == "stopped"
    assert by_gateway["TORA"] == {
        "gateway": "TORA",
        "state": "unavailable",
        "error_code": "RUN_CONTROL_UNAVAILABLE",
    }


def test_control_receipt_is_restart_durable_idempotent_and_input_bound(tmp_path: Path) -> None:
    database = tmp_path / "controls.sqlite"
    first_transport = ControlTransport()
    first = DemoCampaignControls(
        clients=clients(first_transport),
        database=database,
        clock_ns=iter((1_000_000_000, 1_200_000_000)).__next__,
    ).pause_campaign(
        campaign_digest=digest("campaign"),
        idempotency_key="pause-request-0002",
    )

    no_call = NoCallTransport()
    restarted = DemoCampaignControls(
        clients=clients(no_call),
        database=database,
        clock_ns=iter((2_000_000_000, 2_100_000_000)).__next__,
    )
    replay = restarted.pause_campaign(
        campaign_digest=digest("campaign"),
        idempotency_key="pause-request-0002",
    )

    assert replay == first
    assert no_call.calls == 0
    with pytest.raises(ValueError, match="CONTROL_IDEMPOTENCY_CONFLICT"):
        restarted.pause_campaign(
            campaign_digest=digest("different-campaign"),
            idempotency_key="pause-request-0002",
        )
