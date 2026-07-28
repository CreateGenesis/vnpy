from __future__ import annotations

import pytest

from vnpy.demo_web.supervisor_ipc import (
    AuthenticatedSupervisorIpc,
    SecretLeaseBroker,
    SupervisorIpcError,
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
