from __future__ import annotations

import json
import socket
import struct
from datetime import date
from hashlib import sha256
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from vnpy.demo_web.contracts import ServiceName
from vnpy.demo_web.run_clients import BrokerSimulationRunClient, RunClientBinding
from vnpy.demo_web.runtime import (
    ConcreteDemoBackend,
    DemoCandidate,
    build_demo_runtime,
)
from vnpy.demo_web.transport import LengthPrefixedJsonTransport


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def test_runtime_starts_blocked_without_candidate_or_local_services(tmp_path: Path) -> None:
    runtime = build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)

    readiness = runtime.backend.readiness()
    projection = runtime.backend.projection()
    system = runtime.operations.system()

    assert readiness["ready"] is False
    assert readiness["state"] == "blocked"
    assert {item["code"] for item in readiness["blockers"]} == {
        "CANDIDATE_NOT_READY",
        "GATEWAY_NOT_SELECTED",
    }
    assert projection["candidate"]["readiness"] == "unavailable"
    assert projection["current"]["campaign_state"] == "unavailable"
    assert projection["permitted_actions"] == ["emergency_stop"]
    assert runtime.guidance is None
    assert system["configuration"] == {
        "state": "unconfigured",
        "active_version": 0,
        "draft_revision": 0,
    }
    assert len(system["actions"]) >= 20
    assert all(action["action_id"] for action in system["actions"])
    assert len(runtime.bootstrap_fragment_token) >= 32


def test_runtime_uses_remote_supervisor_client_when_address_and_key_are_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[tuple[str, int], bytes]] = []

    class Client:
        def __init__(self, address: tuple[str, int], *, authentication_key: bytes) -> None:
            created.append((address, authentication_key))

        def reconcile(self, service: ServiceName) -> dict[str, object]:
            return {"service": service.value, "state": "stopped", "revision": 0}

        def handle(self, command: dict[str, object]) -> dict[str, object]:
            return command

    monkeypatch.setattr("vnpy.demo_web.runtime.SupervisorIpcClient", Client)
    runtime = build_demo_runtime(
        tmp_path,
        host="127.0.0.1",
        port=8765,
        supervisor_address=("127.0.0.1", 8755),
        supervisor_authentication_key=b"s" * 32,
    )

    assert created == [(('127.0.0.1', 8755), b"s" * 32)]
    assert runtime.operations.system()["services"][0]["state"] == "stopped"


def test_runtime_rejects_malformed_candidate_and_non_loopback_descriptors(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".demo-state"
    state.mkdir()
    (state / "ready-candidate.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="DEMO_CANDIDATE_INVALID"):
        build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)

    candidate = {
        "contract_version": 1,
        "ready": True,
        "candidate_digest": digest("candidate"),
        "author_lineage_digest": digest("author"),
        "package_digest": digest("package"),
        "configuration_digest": digest("configuration"),
        "policy_digest": digest("policy"),
        "symbols": ["600000.SH"],
        "calendar_sessions": [
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
            "2026-07-30",
            "2026-07-31",
        ],
        "lifecycle_revision": 1,
    }
    (state / "ready-candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    endpoint_root = state / "runs" / "XTP"
    endpoint_root.mkdir(parents=True)
    (endpoint_root / "endpoint.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "transport": "tcp-loopback",
                "address": "203.0.113.8:17801",
                "gateway": "XTP",
                "run_digest": digest("run"),
            }
        ),
        encoding="utf-8",
    )

    runtime = build_demo_runtime(tmp_path, host="127.0.0.1", port=8765)
    gateways = {
        item["gateway"]: item for item in runtime.operations.system()["gateways"]
    }
    assert gateways["XTP"]["state"] == "unavailable"


def test_length_prefixed_transport_authenticates_and_round_trips_strict_json() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    address = listener.getsockname()
    captured: list[dict[str, Any]] = []

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            size = struct.unpack(">I", _read_exact(connection, 4))[0]
            request = json.loads(_read_exact(connection, size))
            captured.append(request)
            response = json.dumps({"status": "ok", "echo": request["operation"]}).encode()
            connection.sendall(struct.pack(">I", len(response)) + response)
        listener.close()

    thread = Thread(target=serve)
    thread.start()
    transport = LengthPrefixedJsonTransport("local-secret-token-value-00000001")
    result = transport.request(
        f"tcp://{address[0]}:{address[1]}",
        "demo.side_master.chat.v1",
        {"content": "research only"},
    )
    thread.join(timeout=2)

    assert result == {"status": "ok", "echo": "demo.side_master.chat.v1"}
    assert captured == [
        {
            "kind": "demo_command",
            "transport_version": 1,
            "transport_token": "local-secret-token-value-00000001",
            "operation": "demo.side_master.chat.v1",
            "payload": {"content": "research only"},
        }
    ]


def test_transport_rejects_http_and_remote_endpoints() -> None:
    transport = LengthPrefixedJsonTransport("local-secret-token-value-00000001")
    with pytest.raises(ValueError, match="IPC_LOOPBACK_REQUIRED"):
        transport.request("http://127.0.0.1:17801", "health", {})
    with pytest.raises(ValueError, match="IPC_LOOPBACK_REQUIRED"):
        transport.request("tcp://192.0.2.1:17801", "health", {})


def test_concrete_backend_starts_replays_and_pauses_isolated_runs(tmp_path: Path) -> None:
    transport = RecordingRunTransport()
    clients = {
        gateway: BrokerSimulationRunClient(
            RunClientBinding(
                gateway,
                digest(f"run:{gateway}"),
                f"tcp://127.0.0.1:{17801 if gateway == 'XTP' else 17802}",
            ),
            transport,
        )
        for gateway in ("XTP", "TORA")
    }
    candidate = DemoCandidate(
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
    backend = ConcreteDemoBackend(tmp_path, candidate, clients, guidance_available=True)
    command = {
        "candidate_digest": candidate.candidate_digest,
        "gateways": ["XTP", "TORA"],
        "idempotency_key": "campaign-request-0001",
    }

    assert backend.readiness()["ready"] is True
    first = backend.start_campaign(command)
    replay = backend.start_campaign(command)

    assert first == replay
    assert first["state"] == "active"
    assert [call[1] for call in transport.calls].count("run.prepare_campaign.v1") == 2
    assert [call[1] for call in transport.calls].count("run.start_campaign.v1") == 2
    projection = backend.projection()
    assert projection["current"]["campaign_id"] == first["campaign_id"]
    assert projection["current"]["campaign_state"] == "active"
    paused = backend.pause_campaign(first["campaign_id"])
    assert paused["state"] == "paused"
    assert backend.projection()["current"]["campaign_state"] == "paused"


def test_concrete_backend_contains_every_run_when_start_is_blocked(tmp_path: Path) -> None:
    transport = RecordingRunTransport(blocked_start_gateway="TORA")
    clients = {
        gateway: BrokerSimulationRunClient(
            RunClientBinding(
                gateway,
                digest(f"run:{gateway}"),
                f"tcp://127.0.0.1:{17801 if gateway == 'XTP' else 17802}",
            ),
            transport,
        )
        for gateway in ("XTP", "TORA")
    }
    candidate = DemoCandidate(
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
    backend = ConcreteDemoBackend(tmp_path, candidate, clients, guidance_available=True)

    result = backend.start_campaign(
        {
            "candidate_digest": candidate.candidate_digest,
            "gateways": ["XTP", "TORA"],
            "idempotency_key": "campaign-request-failed-0001",
        }
    )

    assert result["state"] == "stopped"
    operations = [call[1] for call in transport.calls]
    assert operations.count("run.emergency_stop.v1") == 2
    assert backend.projection()["current"]["campaign_state"] == "stopped"


class RecordingRunTransport:
    def __init__(self, blocked_start_gateway: str | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.blocked_start_gateway = blocked_start_gateway

    def request(
        self, endpoint: str, operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((endpoint, operation, payload))
        states = {
            "run.status.v1": "ready",
            "run.prepare_campaign.v1": "prepared",
            "run.start_campaign.v1": "active",
            "run.pause_campaign.v1": "paused",
            "run.emergency_stop.v1": "contained",
        }
        state = states[operation]
        if (
            operation == "run.start_campaign.v1"
            and payload["gateway"] == self.blocked_start_gateway
        ):
            state = "blocked"
        return {
            "contract_version": 1,
            "gateway": payload["gateway"],
            "run_digest": payload["run_digest"],
            "operation": operation,
            "state": state,
            "receipt_digest": digest(f"{operation}:{payload['gateway']}"),
            "data": {
                "connection_state": "connected",
                "reconciliation_state": "complete",
                "positions": [],
                "incidents": [],
                "permitted_next_action": "pause",
            },
        }


def _read_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise RuntimeError("connection closed")
        payload.extend(chunk)
    return bytes(payload)
