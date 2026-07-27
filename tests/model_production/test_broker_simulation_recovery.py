from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from vnpy.model_production.broker_simulation import GatewayBinding
from vnpy.model_production.execution import BrokerSimulationExecutor
from vnpy.model_production.reconciliation import BrokerQueryResult, ReconciliationManager
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
        rpc_endpoint="127.0.0.1:19301",
        state_store_path=str(database),
        created_at_ms=1_000,
        allowed_server_fingerprints=frozenset({server}),
        allowed_account_fingerprints=frozenset({account}),
    )


class TimeoutMainEngine:
    def __init__(self) -> None:
        self.calls = 0

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls += 1
        raise TimeoutError("broker acknowledgement lost")


class NoResendMainEngine:
    def __init__(self) -> None:
        self.calls = 0

    def send_order(self, request: object, gateway_name: str) -> str:
        self.calls += 1
        return "must-not-be-used"


def test_unknown_outcome_survives_restart_is_queried_and_is_never_resent(tmp_path: Path) -> None:
    database = tmp_path / "reconciliation.sqlite"
    timeout_engine = TimeoutMainEngine()
    gateway_binding = binding(database)
    executor = BrokerSimulationExecutor(
        main_engine=timeout_engine,
        binding=gateway_binding,
        reconciliation=ReconciliationManager(database),
    )
    kwargs = {
        "stage": "broker_simulation",
        "request": object(),
        "effect_id": "effect-unknown",
        "operation_key": "operation-unknown",
        "gateway_name": "XTP",
        "account_fingerprint": gateway_binding.account_fingerprint,
        "risk": RiskDecision(True, (), 100),
        "risk_persisted": True,
        "expected_state_matches": True,
    }
    with pytest.raises(TimeoutError):
        executor.submit(**kwargs)
    assert timeout_engine.calls == 1

    restarted = ReconciliationManager(database)
    assert restarted.new_exposure_blocked
    no_resend = NoResendMainEngine()
    restarted_executor = BrokerSimulationExecutor(
        main_engine=no_resend,
        binding=gateway_binding,
        reconciliation=restarted,
    )
    with pytest.raises(PermissionError, match="DUPLICATE_OR_UNCERTAIN_OPERATION"):
        restarted_executor.submit(**kwargs)
    assert no_resend.calls == 0

    queries: list[tuple[str, str | None]] = []
    outcome = restarted.recover_uncertain(
        "effect-unknown",
        lambda operation_key, order_id: (
            queries.append((operation_key, order_id))
            or BrokerQueryResult("accepted", "XTP.order-1", 2)
        ),
    )
    assert outcome.state == "accepted"
    assert outcome.order_id == "XTP.order-1"
    assert queries == [("operation-unknown", None)]
    assert no_resend.calls == 0
    assert restarted.new_exposure_blocked is False


def test_partial_fill_reconnect_uses_original_operation_and_query_only_recovery(tmp_path: Path) -> None:
    database = tmp_path / "partial.sqlite"
    manager = ReconciliationManager(database)
    manager.record_dispatch("effect-partial", "operation-partial")
    manager.record_partial_fill("effect-partial", "XTP.order-partial")
    restarted = ReconciliationManager(database)
    result = restarted.recover_uncertain(
        "effect-partial",
        lambda operation_key, order_id: BrokerQueryResult("accepted", order_id, 3),
    )
    assert result.operation_key == "operation-partial"
    assert result.order_id == "XTP.order-partial"
    assert result.reconciliation_revision == 3
    assert restarted.can_dispatch("operation-partial") is False
