from __future__ import annotations

from hashlib import sha256
from typing import Any

from vnpy.demo_web.run_clients import BrokerSimulationRunClient, RunClientBinding


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self,
        endpoint: str,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.requests.append((endpoint, operation, payload))
        state = "connected" if operation != "run.drain_shutdown.v1" else "stopped"
        return {
            "contract_version": 1,
            "gateway": "XTP",
            "run_digest": digest("run"),
            "operation": operation,
            "state": state,
            "receipt_digest": digest(operation),
            "data": {},
        }


def test_gateway_health_reconnect_and_drain_clients_use_fixed_operations() -> None:
    transport = RecordingTransport()
    client = BrokerSimulationRunClient(
        RunClientBinding("XTP", digest("run"), "tcp://127.0.0.1:17801"),
        transport,
    )

    assert client.gateway_health()["state"] == "connected"
    assert client.reconnect("reconnect-gateway-0001")["state"] == "connected"
    assert client.drain_shutdown("drain-shutdown-0001")["state"] == "stopped"

    assert [item[1] for item in transport.requests] == [
        "run.gateway_health.v1",
        "run.reconnect.v1",
        "run.drain_shutdown.v1",
    ]
    assert transport.requests[0][2] == {
        "contract_version": 1,
        "gateway": "XTP",
        "run_digest": digest("run"),
    }
    assert transport.requests[1][2]["idempotency_key"] == "reconnect-gateway-0001"
    assert transport.requests[2][2]["idempotency_key"] == "drain-shutdown-0001"
