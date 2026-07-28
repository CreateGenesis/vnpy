from __future__ import annotations

from dataclasses import dataclass, field
from threading import Thread

import pytest

from vnpy.demo_web.contracts import ServiceName
from vnpy.demo_web.supervisor_ipc import (
    AuthenticatedSupervisorIpc,
    SecretLeaseBroker,
    SupervisorIpcClient,
    SupervisorIpcError,
    SupervisorIpcServer,
)


def test_authenticated_ipc_rejects_unknown_operation_tampering_and_replay() -> None:
    ipc = AuthenticatedSupervisorIpc(b"i" * 32, clock_ms=lambda: 1_000)
    envelope = ipc.sign(
        "service_control",
        {"service": "research", "action": "start", "expected_revision": 1},
        nonce="nonce-0000000001",
        expires_at_ms=2_000,
    )

    verified = ipc.verify(envelope)
    assert verified == envelope["payload"]
    with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_REPLAY"):
        ipc.verify(envelope)

    tampered = ipc.sign(
        "service_control",
        {"service": "research", "action": "start", "expected_revision": 1},
        nonce="nonce-0000000002",
        expires_at_ms=2_000,
    )
    tampered["payload"]["action"] = "stop"
    with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_AUTHENTICATION_FAILED"):
        ipc.verify(tampered)
    with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_OPERATION_DENIED"):
        ipc.sign("run_command", {"command": "calc.exe"}, nonce="nonce-0000000003", expires_at_ms=2_000)


def test_secret_lease_is_expiring_audience_bound_and_one_use() -> None:
    now = [1_000]
    broker = SecretLeaseBroker(clock_ms=lambda: now[0])
    lease = broker.issue(b"gateway-password", audience="run_xtp", ttl_ms=500)

    with pytest.raises(SupervisorIpcError, match="SECRET_LEASE_AUDIENCE_MISMATCH"):
        broker.consume(lease, audience="agentd")
    assert broker.consume(lease, audience="run_xtp") == b"gateway-password"
    with pytest.raises(SupervisorIpcError, match="SECRET_LEASE_CONSUMED"):
        broker.consume(lease, audience="run_xtp")

    expired = broker.issue(b"rqdata-secret", audience="rqdata_fetcher", ttl_ms=100)
    now[0] += 101
    with pytest.raises(SupervisorIpcError, match="SECRET_LEASE_EXPIRED"):
        broker.consume(expired, audience="rqdata_fetcher")


@dataclass
class FakeSupervisor:
    calls: list[dict[str, object]] = field(default_factory=list)

    def handle(self, command: dict[str, object]) -> dict[str, object]:
        self.calls.append(command)
        return {
            "service": command["service"],
            "state": "ready",
            "revision": int(command["expected_revision"]) + 1,
        }

    def reconcile(self, service: ServiceName) -> dict[str, object]:
        return {"service": service.value, "state": "stopped", "revision": 4}


def test_loopback_ipc_client_dispatches_only_fixed_supervisor_operations() -> None:
    supervisor = FakeSupervisor()
    server = SupervisorIpcServer(
        ("127.0.0.1", 0),
        authentication_key=b"k" * 32,
        supervisor=supervisor,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        client = SupervisorIpcClient((host, port), authentication_key=b"k" * 32)

        assert client.reconcile(ServiceName.RESEARCH) == {
            "service": "research",
            "state": "stopped",
            "revision": 4,
        }
        started = client.handle(
            {"service": "research", "action": "start", "expected_revision": 4}
        )
        assert started["state"] == "ready"
        assert supervisor.calls == [
            {"service": "research", "action": "start", "expected_revision": 4}
        ]
        with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_OPERATION_DENIED"):
            client.request("service_control", {"command": "calc.exe"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_and_secret_lease_payloads_are_strict() -> None:
    ipc = AuthenticatedSupervisorIpc(b"z" * 32, clock_ms=lambda: 1_000)

    with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_OPERATION_DENIED"):
        ipc.sign(
            "health",
            {"service": "research", "path": "C:/forbidden.exe"},
            nonce="nonce-0000000010",
            expires_at_ms=2_000,
        )
    with pytest.raises(SupervisorIpcError, match="SUPERVISOR_IPC_OPERATION_DENIED"):
        ipc.sign(
            "secret_lease",
            {"service": "research", "lease_id": "short"},
            nonce="nonce-0000000011",
            expires_at_ms=2_000,
        )
