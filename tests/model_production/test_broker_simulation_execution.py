from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.model_production.broker_simulation import GatewayBinding
from vnpy.model_production.execution import BrokerSimulationExecutor
from vnpy.model_production.reconciliation import ReconciliationManager
from vnpy.model_production.risk import RiskDecision


def digest(label: str) -> str:
    return f"sha256:{sha256(label.encode()).hexdigest()}"


def binding(database: Path) -> GatewayBinding:
    server = digest("xtp-server")
    account = digest("xtp-account")
    return GatewayBinding.create(
        gateway="XTP",
        environment="broker_simulation",
        server_fingerprint=server,
        account_fingerprint=account,
        credential_ref="credential:xtp",
        process_identity="vnpy-xtp-1",
        rpc_endpoint="127.0.0.1:19201",
        state_store_path=str(database),
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )


class MainEngineStub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls.append((request, gateway_name))
        return "XTP.order-1"


def test_only_exact_bound_main_engine_send_order_dispatches_and_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reconciliation.sqlite"
    main_engine = MainEngineStub()
    gateway_binding = binding(database)
    executor = BrokerSimulationExecutor(
        main_engine=main_engine,
        binding=gateway_binding,
        reconciliation=ReconciliationManager(database),
    )
    request = object()
    accepted = RiskDecision(True, (), 100)
    kwargs = {
        "stage": "broker_simulation",
        "request": request,
        "effect_id": "effect-1",
        "operation_key": "operation-1",
        "gateway_name": "XTP",
        "account_fingerprint": gateway_binding.account_fingerprint,
        "risk": accepted,
        "risk_persisted": True,
        "expected_state_matches": True,
    }
    assert executor.submit(**kwargs) == "XTP.order-1"
    assert executor.submit(**kwargs) == "XTP.order-1"
    assert main_engine.calls == [(request, "XTP")]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"gateway_name": "TORA"}, "GATEWAY_BINDING_MISMATCH"),
        ({"account_fingerprint": digest("other-account")}, "ACCOUNT_BINDING_MISMATCH"),
        ({"risk": RiskDecision(False, ("DENIED",), 0)}, "RISK_NOT_ACCEPTED"),
        ({"risk_persisted": False}, "RISK_NOT_DURABLE"),
        ({"expected_state_matches": False}, "EXPECTED_STATE_MISMATCH"),
    ],
)
def test_wrong_binding_or_non_durable_authority_never_reaches_main_engine(
    tmp_path: Path, changes: dict[str, object], reason: str
) -> None:
    database = tmp_path / f"{reason}.sqlite"
    main_engine = MainEngineStub()
    gateway_binding = binding(database)
    executor = BrokerSimulationExecutor(
        main_engine=main_engine,
        binding=gateway_binding,
        reconciliation=ReconciliationManager(database),
    )
    kwargs = {
        "stage": "broker_simulation",
        "request": object(),
        "effect_id": "effect-1",
        "operation_key": "operation-1",
        "gateway_name": "XTP",
        "account_fingerprint": gateway_binding.account_fingerprint,
        "risk": RiskDecision(True, (), 100),
        "risk_persisted": True,
        "expected_state_matches": True,
    }
    kwargs.update(changes)
    with pytest.raises(PermissionError, match=reason):
        executor.submit(**kwargs)
    assert main_engine.calls == []

