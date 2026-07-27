from __future__ import annotations

from dataclasses import FrozenInstanceError
from hashlib import sha256

import pytest

from vnpy.model_production.broker_simulation import GatewayBinding


def fingerprint(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def create_binding(gateway: str = "XTP") -> GatewayBinding:
    server = fingerprint(f"{gateway}-simulation-server")
    account = fingerprint(f"{gateway}-simulation-account")
    return GatewayBinding.create(
        gateway=gateway,
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref=f"windows-credential:{gateway.lower()}-simulation",
        process_identity=f"vnpy-{gateway.lower()}-process-1",
        rpc_endpoint="127.0.0.1:19001" if gateway == "XTP" else "127.0.0.1:19002",
        state_store_path=f"state/{gateway.lower()}.sqlite",
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )


def test_gateway_binding_is_immutable_allowlisted_and_simulation_only() -> None:
    binding = create_binding()
    assert binding.gateway == "XTP"
    assert binding.environment == "broker_simulation"
    assert binding.rpc_endpoint.startswith("127.0.0.1:")
    assert binding.binding_digest.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        binding.gateway = "TORA"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"environment": "production"}, "SIMULATION_ENVIRONMENT_REQUIRED"),
        ({"server_fingerprint": fingerprint("production-server")}, "SERVER_NOT_ALLOWLISTED"),
        ({"account_fingerprint": fingerprint("production-account")}, "ACCOUNT_NOT_ALLOWLISTED"),
        ({"rpc_endpoint": "0.0.0.0:19001"}, "LOOPBACK_RPC_REQUIRED"),
    ],
)
def test_binding_rejects_production_identity_and_non_loopback_routes(
    change: dict[str, str], reason: str
) -> None:
    server = fingerprint("simulation-server")
    account = fingerprint("simulation-account")
    values = {
        "gateway": "XTP",
        "environment": "broker_simulation",
        "server_fingerprint": server,
        "account_fingerprint": account,
        "credential_ref": "windows-credential:xtp-simulation",
        "process_identity": "vnpy-xtp-process-1",
        "rpc_endpoint": "127.0.0.1:19001",
        "state_store_path": "state/xtp.sqlite",
        "created_at_ms": 1_000,
        "allowed_server_fingerprints": frozenset({server}),
        "allowed_account_fingerprints": frozenset({account}),
    }
    values.update(change)
    with pytest.raises(ValueError, match=reason):
        GatewayBinding.create(**values)

